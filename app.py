import os
import re
import io
import hmac
import hashlib
import secrets
import time
import base64
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime
from functools import wraps
from decimal import Decimal, InvalidOperation

import pymysql
from pymysql.cursors import DictCursor
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, flash, session,
    send_file, abort, jsonify
)
from werkzeug.security import check_password_hash
import qrcode
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

load_dotenv()

def create_app():
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32),
        DEBUG=os.getenv("FLASK_DEBUG", "0") == "1",
        BUSINESS_NAME=os.getenv("BUSINESS_NAME", "VehicleScan"),
        PRODUCT_PRICE=int(os.getenv("PRODUCT_PRICE", "400")),
        COMMISSION=int(os.getenv("PARTNER_COMMISSION_PER_STICKER", "200")),
        BASE_URL=os.getenv("BASE_URL", "http://127.0.0.1:5000").rstrip("/"),
        RAZORPAY_KEY_ID=os.getenv("RAZORPAY_KEY_ID", ""),
        RAZORPAY_KEY_SECRET=os.getenv("RAZORPAY_KEY_SECRET", ""),
        RAZORPAY_WEBHOOK_SECRET=os.getenv("RAZORPAY_WEBHOOK_SECRET", ""),
        SUPPORT_PHONE=os.getenv("SUPPORT_PHONE", ""),
        FREE_DELIVERY_CITY=os.getenv("FREE_DELIVERY_CITY", "Ahmedabad").strip().lower(),
        FREE_DELIVERY_PIN_CODES={p.strip() for p in os.getenv("FREE_DELIVERY_PIN_CODES", "").split(",") if p.strip()},
        DEFAULT_OTHER_DELIVERY_CHARGE=int(os.getenv("DEFAULT_OTHER_DELIVERY_CHARGE", "60")),
        MYSQL_HOST=os.getenv("MYSQL_HOST") or os.getenv("MYSQLHOST", "127.0.0.1"),
        MYSQL_PORT=int(os.getenv("MYSQL_PORT") or os.getenv("MYSQLPORT", "3306")),
        MYSQL_USER=os.getenv("MYSQL_USER") or os.getenv("MYSQLUSER", "root"),
        MYSQL_PASSWORD=os.getenv("MYSQL_PASSWORD") or os.getenv("MYSQLPASSWORD", ""),
        MYSQL_DATABASE=os.getenv("MYSQL_DATABASE") or os.getenv("MYSQLDATABASE", "vehicleconnect_qr"),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "1" if (os.getenv("BASE_URL", "").startswith("https://") or os.getenv("RENDER", "").lower() == "true") else "0") == "1",
        PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)
    if not os.getenv("FLASK_SECRET_KEY"):
        app.logger.warning("FLASK_SECRET_KEY is not configured. Set a long random secret in Render; sessions will reset on restart until configured.")
    login_attempts = {}
    # In-process throttle is a low-cost baseline; for multi-instance deployments use a shared rate-limit store.

    @app.before_request
    def protect_requests():
        if request.method == "POST" and request.endpoint not in {"payment_verify", "razorpay_webhook"}:
            expected = session.get("csrf_token", "")
            supplied = request.form.get("csrf_token", "")
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                abort(400, description="Security token expired. Refresh the page and try again.")

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        if request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' https://checkout.razorpay.com; connect-src 'self' https://api.razorpay.com; frame-src https://api.razorpay.com https://checkout.razorpay.com; base-uri 'self'; form-action 'self' https://api.razorpay.com; frame-ancestors 'none'")
        if request.path.startswith(("/v/", "/admin", "/order/success/", "/payment-qr/", "/track-order")):
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.context_processor
    def inject_csrf():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf_token"]}


    def db():
        return pymysql.connect(
            host=app.config["MYSQL_HOST"],
            port=app.config["MYSQL_PORT"],
            user=app.config["MYSQL_USER"],
            password=app.config["MYSQL_PASSWORD"],
            database=app.config["MYSQL_DATABASE"],
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            ssl_verify_cert=True,
            ssl_verify_identity=True,
            ssl_ca="/etc/ssl/certs/ca-certificates.crt",
        )


    def query_one(sql, args=()):
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchone()
        finally:
            conn.close()

    def query_all(sql, args=()):
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchall()
        finally:
            conn.close()

    def execute(sql, args=()):
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                result = cur.lastrowid
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_additive_schema():
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS app_settings (setting_key VARCHAR(80) PRIMARY KEY, setting_value LONGTEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)")
                cur.execute("CREATE TABLE IF NOT EXISTS delivery_partners (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(120) NOT NULL, phone VARCHAR(20) NULL, tracking_url_template VARCHAR(500) NULL, active TINYINT(1) NOT NULL DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                cur.execute("CREATE TABLE IF NOT EXISTS societies (id BIGINT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(150) NOT NULL UNIQUE, locality VARCHAR(150) NULL, notes VARCHAR(500) NULL, active TINYINT(1) NOT NULL DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                cur.execute("SHOW COLUMNS FROM customers LIKE 'society_id'")
                if not cur.fetchone(): cur.execute("ALTER TABLE customers ADD COLUMN society_id BIGINT NULL")
                cur.execute("SHOW INDEX FROM customers WHERE Key_name='idx_customer_society'")
                if not cur.fetchone(): cur.execute("CREATE INDEX idx_customer_society ON customers(society_id)")
                cur.execute("SHOW COLUMNS FROM orders LIKE 'delivery_partner_id'")
                if not cur.fetchone(): cur.execute("ALTER TABLE orders ADD COLUMN delivery_partner_id INT NULL")
                cur.execute("SHOW COLUMNS FROM orders LIKE 'tracking_number'")
                if not cur.fetchone(): cur.execute("ALTER TABLE orders ADD COLUMN tracking_number VARCHAR(120) NULL")
            conn.commit()
        except Exception:
            conn.rollback(); app.logger.exception("Additive database migration failed; check DB permissions")
        finally:
            conn.close()

    def audit(action, entity_type, entity_id=None, details=None):
        try:
            execute("INSERT INTO audit_logs(admin_id,action,entity_type,entity_id,details) VALUES(%s,%s,%s,%s,%s)",
                    (session.get("admin_id"), action, entity_type, str(entity_id) if entity_id else None, details))
        except Exception:
            app.logger.exception("Could not write audit log")

    def admin_required(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("admin_id"):
                return redirect(url_for("admin_login", next=request.path))
            return fn(*args, **kwargs)
        return wrapped

    def valid_phone(phone):
        digits = re.sub(r"\D", "", phone or "")
        return len(digits) in (10, 12) and (len(digits) != 10 or digits[0] in "6789")

    def make_public_id():
        return secrets.token_hex(16)

    def create_profile(customer_id):
        public_id = make_public_id()
        execute("INSERT INTO qr_profiles(customer_id,public_id,active) VALUES(%s,%s,1)", (customer_id, public_id))
        return public_id

    def qr_png(public_id):
        url = f'{app.config["BASE_URL"]}/v/{public_id}'
        img = qrcode.make(url, box_size=12, border=4)
        out = io.BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        return out

    def qr_pdf(public_id, customer_name):
        png = qr_png(public_id)
        from PIL import Image
        image = Image.open(png)
        qr_buf = io.BytesIO()
        image.save(qr_buf, format="PNG")
        qr_buf.seek(0)
        out = io.BytesIO()
        c = canvas.Canvas(out, pagesize=(90*mm, 55*mm))
        c.setFillColorRGB(0.05, 0.12, 0.22)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(5*mm, 47*mm, app.config["BUSINESS_NAME"])
        c.setFillColorRGB(0.1, 0.55, 0.52)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(5*mm, 41*mm, "SCAN TO CONTACT VEHICLE OWNER")
        c.drawImage(qr_buf, 5*mm, 4*mm, width=36*mm, height=36*mm, preserveAspectRatio=True, mask="auto")
        c.setFillColorRGB(0.12, 0.16, 0.22)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(44*mm, 29*mm, "Need to reach")
        c.drawString(44*mm, 24*mm, "this vehicle owner?")
        c.setFont("Helvetica", 7)
        c.drawString(44*mm, 16*mm, "Scan the QR code")
        c.drawString(44*mm, 12*mm, "with your phone camera.")
        c.setFont("Helvetica", 6)
        c.drawString(5*mm, 1.5*mm, "Use contact details responsibly.")
        c.showPage()
        c.save()
        out.seek(0)
        return out

    def delivery_fee(city, pincode):
        if (city or "").strip().lower() == app.config["FREE_DELIVERY_CITY"] and pincode in app.config["FREE_DELIVERY_PIN_CODES"]:
            return 0
        return app.config["DEFAULT_OTHER_DELIVERY_CHARGE"]

    def razorpay_client():
        if not app.config["RAZORPAY_KEY_ID"] or not app.config["RAZORPAY_KEY_SECRET"]:
            return None
        try:
            import razorpay
            return razorpay.Client(auth=(app.config["RAZORPAY_KEY_ID"], app.config["RAZORPAY_KEY_SECRET"]))
        except Exception:
            app.logger.exception("Razorpay client could not be initialized")
            return None

    try:
        ensure_additive_schema()
    except Exception:
        app.logger.exception("Database startup check failed")

    @app.context_processor
    def inject_globals():
        return {
            "business_name": app.config["BUSINESS_NAME"],
            "support_phone": app.config["SUPPORT_PHONE"],
            "razorpay_key_id": app.config["RAZORPAY_KEY_ID"],
        }

    @app.route("/")
    def home():
        return render_template("home.html", price=app.config["PRODUCT_PRICE"])

    @app.route("/how-it-works")
    def how_it_works():
        return render_template("how.html")

    @app.route("/order", methods=["GET", "POST"])
    def order():
        if request.method == "GET":
            return render_template("order.html", price=app.config["PRODUCT_PRICE"],
                                   delivery_default=app.config["DEFAULT_OTHER_DELIVERY_CHARGE"])
        data = {k: (request.form.get(k) or "").strip() for k in [
            "full_name", "mobile", "whatsapp", "address", "locality", "city",
            "state", "pincode", "vehicle_type", "vehicle_registration", "quantity",
            "payment_method", "partner_id"
        ]}
        errors = []
        if not data["full_name"] or len(data["full_name"]) > 150: errors.append("Enter a valid full name.")
        if not valid_phone(data["mobile"]): errors.append("Enter a valid Indian mobile number.")
        if not data["address"] or len(data["address"]) < 8: errors.append("Enter your complete delivery address.")
        if not data["city"] or not data["state"]: errors.append("City and state are required.")
        if not re.fullmatch(r"\d{6}", data["pincode"]): errors.append("PIN code must contain 6 digits.")
        if data["vehicle_type"] not in ["Car", "Motorcycle", "Scooter", "Other"]: errors.append("Choose a vehicle type.")
        try:
            quantity = int(data["quantity"])
            if quantity < 1 or quantity > 20: raise ValueError()
        except ValueError:
            quantity = 1
            errors.append("Quantity must be between 1 and 20.")
        if data["payment_method"] not in ["COD", "RAZORPAY"]: errors.append("Choose a valid payment method.")
        if not request.form.get("public_consent"):
            errors.append("You must consent to display your name and mobile number on the QR scan page.")
        if data["payment_method"] == "RAZORPAY" and not razorpay_client():
            errors.append("Online payment is not configured yet. Please choose Cash on Delivery or contact the business.")
        if errors:
            for e in errors: flash(e, "error")
            return render_template("order.html", price=app.config["PRODUCT_PRICE"],
                                   delivery_default=app.config["DEFAULT_OTHER_DELIVERY_CHARGE"], form_data=data), 400

        fee = delivery_fee(data["city"], data["pincode"])
        total = app.config["PRODUCT_PRICE"] * quantity + fee
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO customers(full_name,mobile,whatsapp,address,locality,city,state,pincode,
                    vehicle_type,vehicle_registration,public_name_consent,public_phone_consent,whatsapp_consent,consent_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,1,%s,NOW())""",
                    (data["full_name"], data["mobile"], data["whatsapp"] or None, data["address"], data["locality"] or None,
                     data["city"], data["state"], data["pincode"], data["vehicle_type"],
                     data["vehicle_registration"] or None, 1 if data["whatsapp"] and request.form.get("whatsapp_consent") else 0))
                customer_id = cur.lastrowid
                public_id = make_public_id()
                cur.execute("INSERT INTO qr_profiles(customer_id,public_id,active) VALUES(%s,%s,0)", (customer_id, public_id))
                order_number = "VC" + datetime.now().strftime("%y%m%d") + secrets.token_hex(8).upper()
                partner_id = int(data["partner_id"]) if data["partner_id"].isdigit() else None
                cur.execute("""INSERT INTO orders(order_number,customer_id,partner_id,quantity,unit_price,delivery_charge,total_amount,
                    payment_method,payment_status,order_status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'PENDING','PENDING')""",
                    (order_number, customer_id, partner_id, quantity, app.config["PRODUCT_PRICE"], fee, total, data["payment_method"]))
                order_id = cur.lastrowid
                cur.execute("INSERT INTO consent_records(customer_id,consent_type,granted,consent_text) VALUES(%s,'public_name_and_phone',1,%s)",
                    (customer_id, "Customer agreed to display name and phone number on active public QR page."))
                cur.execute("INSERT INTO order_status_history(order_id,old_status,new_status,note) VALUES(%s,NULL,'PENDING','Order placed')", (order_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            app.logger.exception("Order creation failed")
            flash("We could not create your order. Please try again.", "error")
            return redirect(url_for("order"))
        finally:
            conn.close()

        if data["payment_method"] == "RAZORPAY":
            client = razorpay_client()
            try:
                rp_order = client.order.create(data={"amount": total * 100, "currency": "INR", "receipt": order_number,
                                                     "notes": {"order_number": order_number}})
                execute("UPDATE orders SET razorpay_order_id=%s WHERE id=%s", (rp_order["id"], order_id))
                return render_template("payment.html", order_number=order_number, total=total,
                                       rp_order_id=rp_order["id"], key_id=app.config["RAZORPAY_KEY_ID"],
                                       customer=data)
            except Exception:
                app.logger.exception("Razorpay order creation failed")
                execute("UPDATE orders SET payment_status='FAILED' WHERE id=%s", (order_id,))
                flash("Online payment could not be started. Your order is saved but not confirmed as paid. Please contact support or place a COD order.", "error")
                return redirect(url_for("track_order"))

        # QR profile remains inactive until admin confirms the order.
        return redirect(url_for("order_success", order_number=order_number))

    @app.route("/order/success/<order_number>")
    def order_success(order_number):
        order_row = query_one("""SELECT o.order_number,o.quantity,o.total_amount,o.payment_method,o.payment_status,o.order_status,
            c.full_name FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.order_number=%s""", (order_number,))
        if not order_row: abort(404)
        payment_qr = query_one("SELECT setting_key FROM app_settings WHERE setting_key='payment_qr'")
        return render_template("order_success.html", order=order_row, payment_qr_available=bool(payment_qr))

    @app.route("/track-order", methods=["GET", "POST"])
    def track_order():
        result = None
        if request.method == "POST":
            order_number = (request.form.get("order_number") or "").strip().upper()
            mobile = re.sub(r"\D", "", request.form.get("mobile") or "")
            result = query_one("""SELECT o.order_number,o.order_status,o.payment_status,o.payment_method,o.quantity,o.total_amount,o.created_at,
                o.tracking_number,dp.name AS delivery_partner,dp.tracking_url_template
                FROM orders o JOIN customers c ON c.id=o.customer_id LEFT JOIN delivery_partners dp ON dp.id=o.delivery_partner_id
                WHERE o.order_number=%s AND REPLACE(REPLACE(REPLACE(c.mobile,'+',''),' ',''),'-','')=%s""",
                (order_number, mobile))
            if not result: flash("Order not found. Check the order number and mobile number.", "error")
        return render_template("track.html", result=result)

    @app.route("/v/<public_id>")
    def public_profile(public_id):
        profile = query_one("""SELECT q.public_id,q.active AS qr_active,c.full_name,c.mobile,c.whatsapp,
            c.public_name_consent,c.public_phone_consent,c.whatsapp_consent,c.active AS customer_active
            FROM qr_profiles q JOIN customers c ON c.id=q.customer_id WHERE q.public_id=%s""", (public_id,))
        if not profile or not profile["qr_active"] or not profile["customer_active"] or not profile["public_name_consent"] or not profile["public_phone_consent"]:
            return render_template("inactive_qr.html"), 404
        phone_digits = re.sub(r"\D", "", profile["mobile"] or "")
        if len(phone_digits) == 10: phone_digits = "91" + phone_digits
        whatsapp_digits = re.sub(r"\D", "", profile["whatsapp"] or "")
        if len(whatsapp_digits) == 10: whatsapp_digits = "91" + whatsapp_digits
        return render_template("public_profile.html", profile=profile, phone_digits=phone_digits,
                               whatsapp_digits=whatsapp_digits)

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            ip = request.remote_addr or "unknown"
            now = time.time()
            attempts = [t for t in login_attempts.get(ip, []) if now - t < 900]
            if len(attempts) >= 8:
                flash("Too many login attempts. Wait 15 minutes before trying again.", "error")
                return render_template("admin/login.html"), 429
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            admin = query_one("SELECT * FROM admins WHERE username=%s", (username,))
            if admin and check_password_hash(admin["password_hash"], password):
                login_attempts.pop(ip, None)
                session.clear()
                session["csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                session["admin_id"] = admin["id"]
                session["admin_username"] = admin["username"]
                audit("login", "admin", admin["id"])
                next_url = request.args.get("next", "")
                if not next_url.startswith("/") or next_url.startswith("//"):
                    next_url = url_for("admin_dashboard")
                return redirect(next_url)
            attempts.append(now); login_attempts[ip] = attempts
            flash("Invalid username or password.", "error")
        return render_template("admin/login.html")

    @app.route("/admin/logout", methods=["POST"])
    def admin_logout():
        if session.get("admin_id"): audit("logout", "admin", session["admin_id"])
        session.clear()
        return redirect(url_for("admin_login"))

    @app.route("/admin")
    @admin_required
    def admin_dashboard():
        stats = {
            "orders": query_one("SELECT COUNT(*) AS n FROM orders")["n"],
            "pending": query_one("SELECT COUNT(*) AS n FROM orders WHERE order_status IN ('PENDING','CONFIRMED','PROCESSING')")["n"],
            "paid": query_one("SELECT COALESCE(SUM(total_amount),0) AS n FROM orders WHERE payment_status='PAID'")["n"],
            "active_qr": query_one("SELECT COUNT(*) AS n FROM qr_profiles WHERE active=1")["n"],
            "cod": query_one("SELECT COUNT(*) AS n FROM orders WHERE payment_method='COD'")["n"],
            "commission": query_one("SELECT COALESCE(SUM(o.quantity * %s),0) AS n FROM orders o WHERE o.order_status NOT IN ('CANCELLED','REFUNDED')", (app.config["COMMISSION"],))["n"],
        }
        recent = query_all("""SELECT o.*,c.full_name,c.mobile FROM orders o JOIN customers c ON c.id=o.customer_id
            ORDER BY o.created_at DESC LIMIT 10""")
        return render_template("admin/dashboard.html", stats=stats, recent=recent)

    @app.route("/admin/customers")
    @admin_required
    def admin_customers():
        search = (request.args.get("q") or "").strip()
        like = "%" + search + "%"
        customers = query_all("""SELECT c.*,q.public_id,q.active AS qr_active,s.name AS society_name FROM customers c
            LEFT JOIN qr_profiles q ON q.customer_id=c.id
            LEFT JOIN societies s ON s.id=c.society_id
            WHERE c.full_name LIKE %s OR c.mobile LIKE %s ORDER BY c.created_at DESC LIMIT 200""", (like, like))
        return render_template("admin/customers.html", customers=customers, search=search)

    @app.route("/admin/customers/new", methods=["GET", "POST"])
    @admin_required
    def admin_customer_new():
        if request.method == "POST":
            d = {k:(request.form.get(k) or "").strip() for k in ["full_name","mobile","whatsapp","address","locality","city","state","pincode","vehicle_type","vehicle_registration"]}
            society_id = request.form.get("society_id", type=int)
            errors=[]
            if not d["full_name"]: errors.append("Name is required.")
            if not valid_phone(d["mobile"]): errors.append("Enter a valid mobile number.")
            if not d["address"]: errors.append("Address is required.")
            if not d["city"] or not d["state"]: errors.append("City and state are required.")
            if not re.fullmatch(r"\d{6}", d["pincode"]): errors.append("Enter a six-digit PIN code.")
            if not request.form.get("public_consent"): errors.append("Customer consent is required before public QR activation.")
            if errors:
                for e in errors: flash(e,"error")
                return render_template("admin/customer_form.html", d=d, societies=query_all("SELECT id,name FROM societies WHERE active=1 ORDER BY name"))
            conn=db()
            try:
                with conn.cursor() as cur:
                    if society_id:
                        cur.execute("SELECT id FROM societies WHERE id=%s AND active=1", (society_id,))
                        if not cur.fetchone():
                            raise ValueError("Selected society is not available.")
                    cur.execute("""INSERT INTO customers(full_name,mobile,whatsapp,address,locality,city,state,pincode,vehicle_type,
                        vehicle_registration,public_name_consent,public_phone_consent,whatsapp_consent,consent_at,society_id)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,1,%s,NOW(),%s)""",
                        (d["full_name"],d["mobile"],d["whatsapp"] or None,d["address"],d["locality"] or None,d["city"],d["state"],d["pincode"],
                         d["vehicle_type"] or "Other",d["vehicle_registration"] or None,1 if d["whatsapp"] and request.form.get("whatsapp_consent") else 0, society_id))
                    cid=cur.lastrowid
                    pid=make_public_id()
                    cur.execute("INSERT INTO qr_profiles(customer_id,public_id,active) VALUES(%s,%s,1)",(cid,pid))
                    cur.execute("INSERT INTO consent_records(customer_id,consent_type,granted,consent_text) VALUES(%s,'public_name_and_phone',1,%s)",
                        (cid,"Admin recorded customer's consent to display name and mobile number."))
                conn.commit()
                audit("create_customer","customer",cid)
                flash("Customer and QR profile created. Download the sticker below.", "success")
                return redirect(url_for("admin_customer_detail", customer_id=cid))
            except Exception:
                conn.rollback()
                app.logger.exception("Customer creation failed")
                flash("Could not create customer. Check the database and try again.", "error")
            finally:
                conn.close()
        societies = query_all("SELECT id,name FROM societies WHERE active=1 ORDER BY name")
        return render_template("admin/customer_form.html", d={}, societies=societies)

    @app.route("/admin/societies", methods=["GET", "POST"])
    @admin_required
    def admin_societies():
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()[:150]
            locality = (request.form.get("locality") or "").strip()[:150]
            notes = (request.form.get("notes") or "").strip()[:500]
            if not name:
                flash("Society name is required.", "error")
            else:
                try:
                    execute("INSERT INTO societies(name,locality,notes) VALUES(%s,%s,%s)", (name, locality or None, notes or None))
                    audit("create_society", "society", details=name)
                    flash("Society created. You can now assign customers to it.", "success")
                    return redirect(url_for("admin_societies"))
                except Exception as exc:
                    app.logger.warning("Society creation rejected: %s", type(exc).__name__)
                    flash("Could not create this society. Check whether the name already exists.", "error")
        societies = query_all("SELECT s.id,s.name,s.locality,s.notes,s.active,s.created_at,COUNT(c.id) AS customer_count FROM societies s LEFT JOIN customers c ON c.society_id=s.id GROUP BY s.id ORDER BY s.name")
        return render_template("admin/societies.html", societies=societies)

    @app.route("/admin/customers/<int:customer_id>")
    @admin_required
    def admin_customer_detail(customer_id):
        customer=query_one("""SELECT c.*,q.public_id,q.active AS qr_active,s.name AS society_name FROM customers c
            LEFT JOIN qr_profiles q ON q.customer_id=c.id LEFT JOIN societies s ON s.id=c.society_id
            WHERE c.id=%s ORDER BY q.id DESC LIMIT 1""",(customer_id,))
        if not customer: abort(404)
        societies = query_all("SELECT id,name FROM societies WHERE active=1 ORDER BY name")
        return render_template("admin/customer_detail.html", customer=customer, societies=societies)

    @app.route("/admin/customers/<int:customer_id>/society", methods=["POST"])
    @admin_required
    def admin_customer_society(customer_id):
        customer = query_one("SELECT id FROM customers WHERE id=%s", (customer_id,))
        if not customer: abort(404)
        society_id = request.form.get("society_id", type=int)
        if society_id:
            society = query_one("SELECT id FROM societies WHERE id=%s AND active=1", (society_id,))
            if not society:
                flash("Choose an active society.", "error")
                return redirect(url_for("admin_customer_detail", customer_id=customer_id))
        execute("UPDATE customers SET society_id=%s WHERE id=%s", (society_id, customer_id))
        audit("assign_customer_society", "customer", customer_id, f"society_id={society_id or 'unassigned'}")
        flash("Customer society updated.", "success")
        return redirect(url_for("admin_customer_detail", customer_id=customer_id))

    @app.route("/admin/customers/<int:customer_id>/toggle", methods=["POST"])
    @admin_required
    def admin_customer_toggle(customer_id):
        customer=query_one("SELECT * FROM customers WHERE id=%s",(customer_id,))
        if not customer: abort(404)
        new_active=0 if customer["active"] else 1
        execute("UPDATE customers SET active=%s WHERE id=%s",(new_active,customer_id))
        # Deactivation revokes existing tokens. Reactivation never silently revives a revoked QR.
        if not new_active:
            execute("UPDATE qr_profiles SET active=0 WHERE customer_id=%s",(customer_id,))
        audit("toggle_customer","customer",customer_id,f"active={new_active}")
        flash("Customer and QR profile status updated.","success")
        return redirect(url_for("admin_customer_detail",customer_id=customer_id))

    @app.route("/admin/qr/<public_id>/revoke", methods=["POST"])
    @admin_required
    def admin_revoke_qr(public_id):
        row = query_one("SELECT id,customer_id FROM qr_profiles WHERE public_id=%s", (public_id,))
        if not row: abort(404)
        execute("UPDATE qr_profiles SET active=0 WHERE id=%s", (row["id"],))
        audit("revoke_qr", "qr_profile", row["id"], "QR URL permanently deactivated")
        flash("QR code revoked. Scanning this sticker will no longer show the customer profile.", "success")
        return redirect(url_for("admin_customer_detail", customer_id=row["customer_id"]))

    @app.route("/admin/customers/<int:customer_id>/qr.png")
    @admin_required
    def admin_qr_png(customer_id):
        customer=query_one("""SELECT c.full_name,q.public_id FROM customers c JOIN qr_profiles q ON q.customer_id=c.id
            WHERE c.id=%s AND q.active=1 ORDER BY q.id DESC LIMIT 1""",(customer_id,))
        if not customer: abort(404)
        return send_file(qr_png(customer["public_id"]), mimetype="image/png",
                         as_attachment=True, download_name=f"vehiclescan-{customer_id}.png")

    @app.route("/admin/customers/<int:customer_id>/sticker.pdf")
    @admin_required
    def admin_sticker_pdf(customer_id):
        customer=query_one("""SELECT c.full_name,q.public_id FROM customers c JOIN qr_profiles q ON q.customer_id=c.id
            WHERE c.id=%s AND q.active=1 ORDER BY q.id DESC LIMIT 1""",(customer_id,))
        if not customer: abort(404)
        return send_file(qr_pdf(customer["public_id"],customer["full_name"]),mimetype="application/pdf",
                         as_attachment=True,download_name=f"vehiclescan-sticker-{customer_id}.pdf")

    @app.route("/admin/orders")
    @admin_required
    def admin_orders():
        status=(request.args.get("status") or "").strip()
        search=(request.args.get("q") or "").strip()
        sql="""SELECT o.*,c.full_name,c.mobile,c.city,c.pincode FROM orders o JOIN customers c ON c.id=o.customer_id WHERE 1=1"""
        args=[]
        if status:
            sql += " AND o.order_status=%s"; args.append(status)
        if search:
            sql += " AND (o.order_number LIKE %s OR c.full_name LIKE %s OR c.mobile LIKE %s)"
            args += ["%"+search+"%"]*3
        sql += " ORDER BY o.created_at DESC LIMIT 500"
        orders=query_all(sql,args)
        statuses=["PENDING","CONFIRMED","PROCESSING","READY_TO_DISPATCH","SHIPPED","DELIVERED","CANCELLED","REFUNDED"]
        return render_template("admin/orders.html",orders=orders,statuses=statuses,selected_status=status,search=search)

    @app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
    @admin_required
    def admin_order_status(order_id):
        new_status=(request.form.get("order_status") or "").strip()
        allowed={"PENDING","CONFIRMED","PROCESSING","READY_TO_DISPATCH","SHIPPED","DELIVERED","CANCELLED","REFUNDED"}
        if new_status not in allowed: abort(400)
        old=query_one("SELECT order_status,order_number FROM orders WHERE id=%s",(order_id,))
        if not old: abort(404)
        execute("UPDATE orders SET order_status=%s WHERE id=%s",(new_status,order_id))
        execute("INSERT INTO order_status_history(order_id,old_status,new_status,note) VALUES(%s,%s,%s,%s)",
                (order_id,old["order_status"],new_status,"Updated by admin"))
        if new_status in {"CANCELLED","REFUNDED"}:
            execute("UPDATE orders SET commission_status='CANCELLED' WHERE id=%s",(order_id,))
            if new_status=="REFUNDED":
                execute("UPDATE orders SET payment_status='REFUNDED' WHERE id=%s",(order_id,))
        audit("update_order_status","order",order_id,f"{old['order_status']} -> {new_status}")
        flash("Order status updated.","success")
        return redirect(url_for("admin_orders"))

    @app.route("/admin/orders/<int:order_id>/payment", methods=["POST"])
    @admin_required
    def admin_order_payment(order_id):
        row=query_one("SELECT * FROM orders WHERE id=%s",(order_id,))
        if not row: abort(404)
        status=request.form.get("payment_status")
        if status not in {"PENDING","PAID","FAILED","REFUNDED"}: abort(400)
        was_paid = row["payment_status"] == "PAID"
        execute("UPDATE orders SET payment_status=%s WHERE id=%s",(status,order_id))
        if status=="PAID":
            execute("UPDATE orders SET commission_status='APPROVED',order_status=IF(order_status='PENDING','CONFIRMED',order_status) WHERE id=%s",(order_id,))
            if not was_paid:
                execute("UPDATE qr_profiles q JOIN orders o ON o.customer_id=q.customer_id SET q.active=1 WHERE o.id=%s",(order_id,))
        audit("update_payment_status","order",order_id,status)
        flash("Payment status updated.","success")
        return redirect(url_for("admin_orders"))

    @app.route("/admin/partners", methods=["GET","POST"])
    @admin_required
    def admin_partners():
        if request.method=="POST":
            name=(request.form.get("name") or "").strip()
            phone=(request.form.get("phone") or "").strip()
            if not name:
                flash("Partner name is required.","error")
            else:
                execute("INSERT INTO partners(name,phone) VALUES(%s,%s)",(name,phone or None))
                flash("Partner added.","success")
                return redirect(url_for("admin_partners"))
        partners=query_all("""SELECT p.*,
            (SELECT COUNT(*) FROM orders o WHERE o.partner_id=p.id) AS order_count,
            (SELECT COALESCE(SUM(o.quantity),0) FROM orders o WHERE o.partner_id=p.id AND o.order_status NOT IN ('CANCELLED','REFUNDED')) AS units,
            (SELECT COALESCE(SUM(o.quantity * %s),0) FROM orders o WHERE o.partner_id=p.id AND o.commission_status IN ('APPROVED','PAID')) AS approved_commission
            FROM partners p ORDER BY p.created_at DESC""",(app.config["COMMISSION"],))
        return render_template("admin/partners.html",partners=partners,commission=app.config["COMMISSION"])

    @app.route("/admin/partners/<int:partner_id>/commission-paid", methods=["POST"])
    @admin_required
    def admin_partner_paid(partner_id):
        execute("UPDATE orders SET commission_status='PAID' WHERE partner_id=%s AND commission_status='APPROVED'",(partner_id,))
        audit("partner_commission_paid","partner",partner_id)
        flash("Approved commissions marked as paid.","success")
        return redirect(url_for("admin_partners"))

    @app.route("/admin/delivery", methods=["GET", "POST"])
    @admin_required
    def admin_couriers():
        if request.method == "POST":
            action = request.form.get("action", "add_courier")
            if action == "add_courier":
                name = (request.form.get("name") or "").strip()[:120]
                phone = (request.form.get("phone") or "").strip()[:20]
                template = (request.form.get("tracking_url_template") or "").strip()[:500]
                if not name:
                    flash("Delivery partner name is required.", "error")
                elif template and (not template.startswith("https://") or "{tracking}" not in template):
                    flash("Tracking URL must start with https:// and include {tracking} where the tracking number belongs.", "error")
                else:
                    execute("INSERT INTO delivery_partners(name,phone,tracking_url_template) VALUES(%s,%s,%s)", (name, phone or None, template or None))
                    audit("add_delivery_partner", "delivery_partner", details=name)
                    flash("Delivery partner added.", "success")
                return redirect(url_for("admin_couriers"))
            if action == "assign_delivery":
                order_id = request.form.get("order_id", type=int)
                courier_id = request.form.get("delivery_partner_id", type=int)
                tracking = (request.form.get("tracking_number") or "").strip()[:120]
                if not order_id:
                    abort(400)
                if courier_id and not query_one("SELECT id FROM delivery_partners WHERE id=%s AND active=1", (courier_id,)):
                    flash("Choose an active delivery partner.", "error")
                else:
                    execute("UPDATE orders SET delivery_partner_id=%s,tracking_number=%s WHERE id=%s", (courier_id or None, tracking or None, order_id))
                    audit("assign_delivery", "order", order_id, f"courier={courier_id}; tracking={tracking}")
                    flash("Delivery details saved.", "success")
                return redirect(url_for("admin_couriers"))
        couriers = query_all("SELECT * FROM delivery_partners ORDER BY active DESC,name")
        orders = query_all("SELECT o.id,o.order_number,o.order_status,o.delivery_partner_id,o.tracking_number,c.full_name,c.city,c.pincode FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.order_status NOT IN ('DELIVERED','CANCELLED','REFUNDED') ORDER BY o.created_at DESC LIMIT 200")
        return render_template("admin/delivery.html", couriers=couriers, orders=orders)

    @app.route("/admin/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings():
        if request.method == "POST":
            upload = request.files.get("payment_qr")
            if upload and upload.filename:
                if upload.mimetype not in {"image/png", "image/jpeg", "image/webp"}:
                    flash("Upload a PNG, JPG or WEBP payment QR image.", "error")
                    return redirect(url_for("admin_settings"))
                raw = upload.read(1024 * 1024 + 1)
                if not raw or len(raw) > 1024 * 1024:
                    flash("Payment QR image must be under 1 MB.", "error")
                    return redirect(url_for("admin_settings"))
                try:
                    from PIL import Image
                    image = Image.open(io.BytesIO(raw)); image.verify()
                    image = Image.open(io.BytesIO(raw)).convert("RGB")
                    out = io.BytesIO(); image.save(out, format="PNG", optimize=True)
                    encoded = "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")
                    execute("INSERT INTO app_settings(setting_key,setting_value) VALUES('payment_qr',%s) ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)", (encoded,))
                    audit("update_payment_qr", "settings")
                    flash("Payment QR saved in the database.", "success")
                except Exception:
                    app.logger.exception("Payment QR upload rejected")
                    flash("That image could not be processed. Upload a valid QR image.", "error")
            elif request.form.get("remove_payment_qr") == "1":
                execute("DELETE FROM app_settings WHERE setting_key='payment_qr'")
                audit("remove_payment_qr", "settings")
                flash("Payment QR removed.", "success")
            return redirect(url_for("admin_settings"))
        saved = query_one("SELECT setting_value FROM app_settings WHERE setting_key='payment_qr'")
        return render_template("admin/settings.html", payment_qr=saved["setting_value"] if saved else None, price=app.config["PRODUCT_PRICE"], commission=app.config["COMMISSION"],
                               free_city=app.config["FREE_DELIVERY_CITY"].title(),
                               pins=", ".join(sorted(app.config["FREE_DELIVERY_PIN_CODES"])),
                               delivery=app.config["DEFAULT_OTHER_DELIVERY_CHARGE"],
                               razorpay_enabled=bool(razorpay_client()))

    @app.route("/payment-qr/<order_number>")
    def payment_qr(order_number):
        saved = query_one("SELECT setting_value FROM app_settings WHERE setting_key='payment_qr'")
        order_row = query_one("SELECT order_number,total_amount,payment_status FROM orders WHERE order_number=%s", (order_number,))
        if not saved or not order_row: abort(404)
        return render_template("payment_qr.html", payment_qr_image=saved["setting_value"], order=order_row)

    @app.route("/payment/verify", methods=["POST"])
    def payment_verify():
        payload=request.get_json(silent=True) or {}
        order_number=(payload.get("order_number") or "").strip()
        rp_order_id=payload.get("razorpay_order_id","")
        rp_payment_id=payload.get("razorpay_payment_id","")
        signature=payload.get("razorpay_signature","")
        order_row=query_one("SELECT * FROM orders WHERE order_number=%s AND razorpay_order_id=%s",(order_number,rp_order_id))
        if not order_row or not app.config["RAZORPAY_KEY_SECRET"]:
            return jsonify({"ok":False,"message":"Order or payment configuration not found."}),400
        expected=hmac.new(app.config["RAZORPAY_KEY_SECRET"].encode(),
                          f"{rp_order_id}|{rp_payment_id}".encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature):
            execute("UPDATE orders SET payment_status='FAILED' WHERE id=%s",(order_row["id"],))
            return jsonify({"ok":False,"message":"Payment signature verification failed."}),400
        if order_row["payment_status"] != "PAID":
            execute("UPDATE orders SET payment_status='PAID',razorpay_payment_id=%s,order_status='CONFIRMED',commission_status='APPROVED' WHERE id=%s",
                    (rp_payment_id,order_row["id"]))
            execute("UPDATE qr_profiles SET active=1 WHERE customer_id=%s",(order_row["customer_id"],))
        audit("razorpay_payment_verified","order",order_row["id"],rp_payment_id)
        return jsonify({"ok":True,"redirect":url_for("order_success",order_number=order_number)})

    @app.route("/razorpay/webhook", methods=["POST"])
    def razorpay_webhook():
        secret=app.config["RAZORPAY_WEBHOOK_SECRET"]
        if not secret:
            return jsonify({"ok":False,"message":"Webhook secret not configured."}),503
        body=request.get_data()
        signature=request.headers.get("X-Razorpay-Signature","")
        expected=hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature):
            return jsonify({"ok":False}),400
        import json
        event=json.loads(body or b"{}")
        if event.get("event")=="payment.captured":
            entity=event.get("payload",{}).get("payment",{}).get("entity",{})
            rp_order_id=entity.get("order_id")
            rp_payment_id=entity.get("id")
            row=query_one("SELECT * FROM orders WHERE razorpay_order_id=%s",(rp_order_id,))
            if row and row["payment_status"]!="PAID":
                execute("UPDATE orders SET payment_status='PAID',razorpay_payment_id=%s,order_status='CONFIRMED',commission_status='APPROVED' WHERE id=%s",
                        (rp_payment_id,row["id"]))
                execute("UPDATE qr_profiles SET active=1 WHERE customer_id=%s",(row["customer_id"],))
        return jsonify({"ok":True})

    @app.route("/health")
    def health():
        return jsonify({"status":"ok","app":app.config["BUSINESS_NAME"]})

    @app.errorhandler(500)
    def internal_error(error):
        return render_template("error.html", code=500, message="Something went wrong. Check the terminal log for details."),500

    return app
