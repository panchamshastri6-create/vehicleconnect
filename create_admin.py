import getpass
import os
from dotenv import load_dotenv
import pymysql
from werkzeug.security import generate_password_hash

load_dotenv()

def main():
    username = input("Admin username: ").strip()
    if not username:
        print("Username cannot be empty.")
        return
    password = getpass.getpass("Admin password (minimum 12 characters): ")
    if len(password) < 12:
        print("Use at least 12 characters.")
        return
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST","127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT","3306")),
        user=os.getenv("MYSQL_USER","root"),
        password=os.getenv("MYSQL_PASSWORD",""),
        database=os.getenv("MYSQL_DATABASE","vehicleconnect_qr"),
        charset="utf8mb4"
    )
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO admins(username,password_hash) VALUES(%s,%s) ON DUPLICATE KEY UPDATE password_hash=VALUES(password_hash)",
                        (username,generate_password_hash(password)))
        conn.commit()
        print("Admin account created/updated. Keep the password private.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
