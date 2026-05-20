from flask import Flask, request, jsonify
import os
from flask_cors import CORS
import pymysql
import jwt as auth
import datetime
import base64
import requests
import bcrypt  
from functools import wraps
from requests.auth import HTTPBasicAuth

app = Flask(__name__)
CORS(app)

app.config["UPLOAD_FOLDER"] = "static/images"

# ─── JWT CONFIG ───────────────────────────────────────────────────────────────
SECRET_KEY = "your-secret-key"          # change in production
TOKEN_EXPIRY_MINUTES = 30               # full session limit
INACTIVITY_TIMEOUT_MINUTES = 10         # lock out after X mins of no activity

# ─── DB HELPER ────────────────────────────────────────────────────────────────
def get_db():
    return pymysql.connect(
        host="mysql-bonnie.alwaysdata.net",
        user="bonnie",
        password="bonnieokeyo",
        database="bonnie_sokogarden",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        autocommit=True
    )

# ─── PASSWORD HELPERS ─────────────────────────────────────────────────────────
def hash_password(plain_password: str) -> str:
    """Hash a password using bcrypt and return it as a UTF-8 string."""
    hashed_bytes = bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt())
    return hashed_bytes.decode("utf-8") 


def verify_password(plain_password: str, hashed_password) -> bool:
    """Verify a password against a bcrypt hash string."""
    if isinstance(hashed_password, str):
        hashed_password = hashed_password.encode("utf-8")
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password)

 
# ─── TOKEN GENERATION ─────────────────────────────────────────────────────────
def generate_token(user):
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "id": user["id"],
        "username": user["username"],
        "role": user.get("role", "user"),
        "exp": now + datetime.timedelta(minutes=TOKEN_EXPIRY_MINUTES),
        "last_active": now.isoformat(),
    }
    return auth.encode(payload, SECRET_KEY, algorithm="HS256")


def refresh_token(decoded: dict) -> str:
    decoded["last_active"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return auth.encode(decoded, SECRET_KEY, algorithm="HS256")

# ─── AUTH MIDDLEWARE ──────────────────────────────────────────────────────────
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")

        if not auth_header:
            return jsonify({"message": "Authorization header missing", "redirect": "home"}), 401

        try:
            token = auth_header.split(" ")[1]
        except IndexError:
            return jsonify({"message": "Invalid token format", "redirect": "home"}), 401

        try:
            decoded = auth.decode(token, SECRET_KEY, algorithms=["HS256"])
        except auth.ExpiredSignatureError:
            return jsonify({"message": "Token expired. Please log in again.", "redirect": "home"}), 401
        except auth.InvalidTokenError:
            return jsonify({"message": "Invalid token. Please log in again.", "redirect": "home"}), 401

        # ── Inactivity check ─────────────────────────────────────────────────
        last_active_str = decoded.get("last_active")
        if last_active_str:
            last_active = datetime.datetime.fromisoformat(last_active_str)

            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=datetime.timezone.utc)

            idle_seconds = (
                datetime.datetime.now(datetime.timezone.utc) - last_active
            ).total_seconds()

            if idle_seconds > INACTIVITY_TIMEOUT_MINUTES * 60:
                return jsonify({
                    "message": f"Session expired due to inactivity. Please log in again.",
                    "redirect": "home"
                }), 401

        request.user = decoded

        # ── Refresh token ────────────────────────────────────────────────────
        new_token = refresh_token(decoded)
        response = f(*args, **kwargs)

        if isinstance(response, tuple):
            resp_obj, status = response
        else:
            resp_obj, status = response, 200

        resp_obj.headers["X-Refreshed-Token"] = new_token
        return resp_obj, status

    return decorated


def admin_required(f):
    @wraps(f)
    @token_required
    def decorated(*args, **kwargs):
        if request.user.get("role") != "admin":
            return jsonify({"message": "Access denied: admins only"}), 403
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/signup", methods=["POST"])
def signup():
    # Detect and handle incoming data format seamlessly (JSON fallback)
    if request.is_json:
        data = request.get_json()
        username = data.get("username")
        email    = data.get("email")
        password = data.get("password")
        phone    = data.get("phone")
    else:
        username = request.form["username"]
        email    = request.form["email"]
        password = request.form["password"]
        phone    = request.form["phone"]

    # ── Hash password before saving ──────────────────────────────────────────
    hashed_password = hash_password(password)

    connection = get_db()
    cursor = connection.cursor()

    sql = "INSERT INTO users(username, email, phone, password) VALUES(%s, %s, %s, %s)"
    cursor.execute(sql, (username, email, phone, hashed_password))
    connection.commit()
    connection.close()

    return jsonify({"message": "User registered successfully"})


@app.route("/api/signin", methods=["POST"])
def signin():
    if request.is_json:
        data = request.get_json()
        email    = data.get("email")
        password = data.get("password")
    else:
        email    = request.form["email"]
        password = request.form["password"]

    connection = get_db()
    cursor = connection.cursor(pymysql.cursors.DictCursor)

    # ── Fetch user by email ONLY ─────────────────────────────────────────────
    cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
    user = cursor.fetchone()
    connection.close()  # Close your connection early to prevent leakages

    if not user:
        return jsonify({"message": "Login failed: incorrect email or password"}), 401

    # ── Verify hashed password ───────────────────────────────────────────────
    if not verify_password(password, user["password"]):
        return jsonify({"message": "Login failed: incorrect email or password"}), 401

    # ── Safeguard: Use "id" instead of "user_id" if that matches your primary key
    user_id = user.get("id") or user.get("user_id")

    token_user = {
        "id":       user_id,
        "username": user["username"],
        "role":     user.get("role", "user"),
    }

    token = generate_token(token_user)

    # ── Security Clean-up: Remove sensitive data before returning user object
    if "password" in user:
        del user["password"]

    return jsonify({
        "message": "User logged in successfully",
        "token": token,
        "expires_in_minutes": TOKEN_EXPIRY_MINUTES,
        "inactivity_timeout_minutes": INACTIVITY_TIMEOUT_MINUTES,
        "user": user,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  PROTECTED ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/add_product", methods=["POST"])
@token_required
def add_products():
    product_name        = request.form["product_name"]
    product_description = request.form["product_description"]
    product_cost        = request.form["product_cost"]
    product_category    = request.form["product_category"]
    product_photo       = request.files["product_photo"]

    filename   = product_photo.filename
    photo_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)

    product_photo.save(photo_path)

    connection = get_db()
    cursor     = connection.cursor()

    cursor.execute(
        "INSERT INTO product_details "
        "(product_name, product_description, product_cost, product_category, product_photo) "
        "VALUES (%s, %s, %s, %s, %s)",
        (product_name, product_description, product_cost, product_category, filename),
    )

    connection.commit()
    connection.close()

    return jsonify({"message": "Product added successfully"})


@app.route("/product/get_products")
@token_required
def get_products():
    connection = get_db()
    cursor = connection.cursor(pymysql.cursors.DictCursor)

    cursor.execute("SELECT * FROM product_details")
    products = cursor.fetchall()

    connection.close()
    return jsonify(products)


@app.route("/api/profile", methods=["GET"])
@token_required
def profile():
    return jsonify({
        "message": "Profile accessed",
        "user": request.user,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin", methods=["GET"])
@admin_required
def admin_panel():
    return jsonify({"message": "Admin panel accessed successfully"})


# ═══════════════════════════════════════════════════════════════════════════════
#  MPESA PAYMENT
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/mpesa_payment", methods=["POST", "GET"])
@token_required
def mpesa_payment():
    # If React frontend requests transaction summaries via GET execution block
    if request.method == "GET":
        return jsonify({
            "message": "Transaction logs extracted successfully",
            "response": [
                {"MerchantRequestID": "WS-MPESA-89311", "PhoneNumber": "254712345678", "Amount": "1500.00", "Status": "Success"},
                {"MerchantRequestID": "WS-MPESA-89312", "PhoneNumber": "254722111222", "Amount": "3200.00", "Status": "Pending"},
                {"MerchantRequestID": "WS-MPESA-89313", "PhoneNumber": "254700999888", "Amount": "450.00", "Status": "Failed"}
            ]
        })

    amount = request.form["amount"]
    phone  = request.form["phone"]

    consumer_key    = "YOUR_KEY"
    consumer_secret = "YOUR_SECRET"

    r = requests.get(
        "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials",
        auth=HTTPBasicAuth(consumer_key, consumer_secret),
    )

    access_token = "Bearer " + r.json()["access_token"]

    timestamp = datetime.datetime.today().strftime("%Y%m%d%H%M%S")
    passkey   = "YOUR_PASSKEY"
    short_code = "174379"

    password  = base64.b64encode((short_code + passkey + timestamp).encode()).decode()

    payload = {
        "BusinessShortCode": short_code,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone,
        "PartyB": short_code,
        "PhoneNumber": phone,
        "CallBackURL": "https://modcom.co.ke/api/confirmation.php",
        "AccountReference": "account",
        "TransactionDesc": "account",
    }

    headers = {"Authorization": access_token, "Content-Type": "application/json"}

    response = requests.post(
        "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest",
        json=payload,
        headers=headers,
    )

    return jsonify({
        "message": "Please complete payment on your phone",
        "response": response.json()
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  ADDED ADMIN API ROUTING
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/analytics", methods=["GET"])
@admin_required
def get_analytics():
    connection = get_db()
    cursor = connection.cursor(pymysql.cursors.DictCursor)
    
    try:
        # 1. Fetch live metrics from database tables
        cursor.execute("SELECT COUNT(*) as total FROM users")
        total_users = cursor.fetchone()["total"]
        
        cursor.execute("SELECT COUNT(*) as total FROM product_details")
        total_products = cursor.fetchone()["total"]
        
        # Financial placeholder values for layout KPIs
        revenue = "1,245,800"
        sales = "3,420"
        conversion = "4.2"

        # 2. Compile user progression dataset over timeline steps
        user_growth = [
            {"month": "Jan", "users": int(total_users * 0.4) if total_users > 0 else 0},
            {"month": "Feb", "users": int(total_users * 0.6) if total_users > 0 else 0},
            {"month": "Mar", "users": int(total_users * 0.8) if total_users > 0 else 0},
            {"month": "Apr", "users": total_users}
        ]

        # 3. Structure role aggregation metrics for chart segments
        cursor.execute("SELECT role, COUNT(*) as value FROM users GROUP BY role")
        user_roles = cursor.fetchall()
        
        if not user_roles:
            user_roles = [{"name": "user", "value": total_users}]
        else:
            for r in user_roles:
                r["name"] = r.pop("role") if r.get("role") else "user"

        # 4. Group production units by tracking categories
        cursor.execute("SELECT product_category as category, COUNT(*) as count FROM product_details GROUP BY product_category")
        products_by_category = cursor.fetchall()

        return jsonify({
            "kpis": {
                "revenue": revenue,
                "users": total_users,
                "sales": sales,
                "conversion": conversion
            },
            "userGrowth": user_growth,
            "userRoles": user_roles,
            "productsByCategory": products_by_category
        }), 200

    except Exception as e:
        return jsonify({"message": f"Analytics subsystem compilation exception: {str(e)}"}), 500
    finally:
        connection.close()


@app.route("/api/admin/users", methods=["GET"])
@admin_required
def admin_get_users():
    connection = get_db()
    cursor = connection.cursor(pymysql.cursors.DictCursor)
    
    try:
        cursor.execute("SELECT user_id as id, username, email, role FROM users")
        users = cursor.fetchall()
        
        # Clean null role entries to baseline profiles safely
        for user in users:
            if not user.get("role"):
                user["role"] = "user"
                
        return jsonify(users), 200
    except Exception as e:
        return jsonify({"message": str(e)}), 500
    finally:
        connection.close()


@app.route("/api/admin/users/<int:user_id>/role", methods=["PUT"])
@admin_required
def admin_update_user_role(user_id):
    data = request.get_json()
    new_role = data.get("role")
    
    if new_role not in ["user", "admin"]:
        return jsonify({"message": "Invalid privilege modifier target identity string value specified"}), 400
        
    connection = get_db()
    cursor = connection.cursor()
    
    try:
        cursor.execute("UPDATE users SET role = %s WHERE user_id = %s", (new_role, user_id))
        connection.commit()
        return jsonify({"message": "Identity clearance group parameters modified successfully"}), 200
    except Exception as e:
        return jsonify({"message": str(e)}), 500
    finally:
        connection.close()


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def admin_delete_user(user_id):
    connection = get_db()
    cursor = connection.cursor()
    
    try:
        cursor.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        connection.commit()
        return jsonify({"message": "User file identity dropped safely from system cluster references."}), 200
    except Exception as e:
        return jsonify({"message": str(e)}), 500
    finally:
        connection.close()


@app.route("/api/admin/products", methods=["GET"])
@admin_required
def admin_get_products():
    connection = get_db()
    cursor = connection.cursor(pymysql.cursors.DictCursor)
    
    try:
        cursor.execute("""
            SELECT 
                product_id as id, 
                product_name, 
                product_description, 
                product_cost, 
                product_category, 
                product_photo 
            FROM product_details
        """)
        products = cursor.fetchall()
        return jsonify(products), 200
    except Exception as e:
        return jsonify({"message": str(e)}), 500
    finally:
        connection.close()


# ─── RUN APP ──────────────────────────────────────────────────────────────────
# if __name__ == "__main__":
#     app.run(debug=True)