# VehicleConnect QR — Local Windows Starter

A Flask + MySQL starter for a vehicle QR-sticker business. It includes a public storefront, manual admin customer entry, unique QR profiles, print-ready QR PNG/PDF, order management, COD, optional Razorpay checkout, and partner commission tracking.

## Important before using with real customers

- This is a locally runnable starter, not a security-audited production deployment.
- Keep the QR page limited to a customer's consented name and phone number. Addresses are private.
- The sample Ahmedabad PIN-code list is only a starting configuration. Verify your delivery coverage before advertising free delivery.
- Razorpay requires your own account and API keys. Do not accept real payments until testing and verification are complete.
- Local URLs (`127.0.0.1`) only work on your PC. QR codes printed from local testing will not work for customers scanning them over the internet. Before producing customer stickers, deploy the app to a stable HTTPS domain and update `BASE_URL`.
- Public QR pages show contact details to anyone scanning an active sticker. Obtain explicit consent and deactivate profiles on request.
- Have the privacy, terms, refund and consumer-facing policies reviewed for your actual business.

## Requirements

- Windows 10/11
- Python 3.11 or 3.12 recommended
- MySQL Server 8.x (or compatible MySQL)
- Optional: Razorpay account for online payments

## 1. Extract the ZIP

Extract `vehicleconnect-qr.zip` to a folder, for example:

`C:\Users\YOURNAME\Downloads\vehicleconnect-qr`

Open PowerShell in that folder.

## 2. Create a virtual environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation, run this only in the current PowerShell window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 3. Configure MySQL

Start the MySQL service. Open MySQL Workbench or the MySQL command-line client and run the SQL in `schema.sql`.

If you use the command line:

```powershell
mysql -u root -p < schema.sql
```

If your local root account has no password, press Enter when prompted. Do not use an empty database password on a publicly exposed server.

## 4. Configure environment

```powershell
Copy-Item .env.example .env
notepad .env
```

Set your real MySQL password if required. Set a long random `FLASK_SECRET_KEY`. Keep `BASE_URL=http://127.0.0.1:5000` for local development only.

Do not share your `.env` file or commit it to GitHub.

## 5. Create an admin account

```powershell
python create_admin.py
```

Enter a username and a strong password of at least 12 characters.

## 6. Run the website

```powershell
python run.py
```

Open:

- Storefront: http://127.0.0.1:5000
- Admin login: http://127.0.0.1:5000/admin/login
- Health check: http://127.0.0.1:5000/health

Sign in, select **Add Customer**, enter the customer's details, record their consent, and create the profile. From the customer detail page, download the QR PNG or print-ready PDF.

## 7. Test the QR profile

For local development, open the profile URL on the same PC. A phone cannot normally reach `127.0.0.1` on your PC because that address refers to the phone itself. For phone testing on the same Wi-Fi, you can temporarily set `BASE_URL` to your PC's local LAN IP and run Flask with a LAN bind, but never expose the debug server to the public internet. For real customer stickers, use a public HTTPS domain.

## 8. Razorpay test mode

1. Create a Razorpay account and use its Test Mode keys.
2. Put `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` in `.env`.
3. Restart the app.
4. Test checkout using Razorpay's current official test credentials/instructions.
5. Configure the webhook URL as `https://YOURDOMAIN/razorpay/webhook` and set the matching webhook secret.
6. Test payment failure, success, repeated callbacks, and order status changes before enabling live mode.

The app verifies the Razorpay checkout signature on the server. The webhook endpoint verifies the webhook signature. Use HTTPS for real payments.

## Business configuration

The `.env` file includes:
- `PRODUCT_PRICE=400`
- `PARTNER_COMMISSION_PER_STICKER=200`
- `FREE_DELIVERY_CITY=Ahmedabad`
- `FREE_DELIVERY_PIN_CODES=...`
- `DEFAULT_OTHER_DELIVERY_CHARGE=60`

The delivery charge is free only when both city and PIN code match the configured list. Confirm the list is accurate for your courier coverage. The starter uses the commission setting in dashboard reporting; confirm actual payouts against completed, eligible orders.

## Useful routes

- `/` — storefront
- `/order` — customer order form
- `/track-order` — order tracking
- `/v/<public_id>` — public QR profile
- `/admin` — admin dashboard
- `/admin/customers/new` — manually create customer
- `/admin/orders` — manage orders
- `/admin/partners` — partner and commission reporting
- `/admin/settings` — current settings display

## Troubleshooting

**Can't connect to MySQL:** Check that MySQL is running, `.env` credentials are correct, and `schema.sql` has been executed.

**Unknown database:** Create the database with `schema.sql`.

**Module not found:** Activate `.venv` and run `pip install -r requirements.txt`.

**QR links don't work after printing:** Local QR links are only for development. Deploy to HTTPS and set `BASE_URL` before printing stickers for customers.

**Online payment unavailable:** Keep COD enabled and check the Razorpay keys. The website deliberately does not claim that payment succeeded if payment setup is missing.

## Production checklist

Before public launch:
- Deploy behind HTTPS using a production WSGI server.
- Use a least-privilege MySQL user and strong credentials.
- Set `FLASK_DEBUG=0` and a strong secret key.
- Configure a public HTTPS `BASE_URL`.
- Verify delivery PIN codes, policies, and support contact.
- Configure payment webhooks and test the payment lifecycle.
- Add database backups, monitoring, log rotation, and a privacy/consent request process.
- Add CSRF protection and rate limiting at the production edge/application layer before launch.
- Conduct a security review. Do not run Flask's debug server on a public interface.
