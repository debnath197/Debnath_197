import os
import re
import csv
import io
import json
from datetime import datetime, timedelta
import tempfile
import random
import smtplib
from email.mime.text import MIMEText

import geopandas as gpd      # shapefile পড়ার জন্য
import requests              # weather + OSM Overpass API এর জন্য

from flask import (
    Flask,
    render_template,
    request,
    Response,
    redirect,
    url_for,
    session,
    jsonify,
)

app = Flask(__name__)
app.secret_key = "change_this_to_a_random_secret_key"  # session এর জন্য

# ===================== USER STORE (PERSISTENT) =====================

USERS_FILE = "users.json"   # phone/email/password এই ফাইলে সেভ হবে
users = {}                  # key = phone, value = dict(phone, email, password)


def load_users():
    """users.json থেকে ইউজার লোড করি"""
    global users
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                users = json.load(f)
        except Exception as e:
            print("Failed to load users.json:", e)
            users = {}
    else:
        users = {}


def save_users():
    """users.json এ ইউজার সেভ করি"""
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Failed to save users.json:", e)


# সার্ভার স্টার্টে লোড
load_users()

# ============ OTP STORE + EMAIL CONFIG ============

otp_store = {}  # key = email.lower(), value = {"otp": "123456", "expires_at": datetime, "purpose": "login"/"reset"}

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# 🔴 এখানেই নিজের Gmail + App Password বসাবে
# Gmail এ 2-Step Verification ON করে App Password জেনারেট করতে হবে
SMTP_USER = "debnathghosh949@gmail.com"       # উদাহরণ: "mygeoportal@gmail.com"
SMTP_PASS = "fvlmelptanfyvisq"  # উদাহরণ: "abcdijklmnopqrxy"


def generate_otp():
    return "{:06d}".format(random.randint(0, 999999))


def send_otp_email(to_email, otp, purpose="login"):
    """
    Gmail দিয়ে OTP পাঠাবে।
    যেই ইমেলে user login/forgot form এ email লিখবে, সেই to_email এ OTP যাবে।
    return: (success: bool, error_message: str | None)
    """
    subject = "GeoPortal OTP"
    body = f"Your GeoPortal {purpose} OTP is: {otp}\nThis OTP is valid for 5 minutes."

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to_email

    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_email, msg.as_string())
        server.quit()

        print(f"[EMAIL] OTP sent to {to_email}", flush=True)
        return True, None

    except Exception as e:
        # ইমেল গেলে না, অন্তত console এ OTP দেখাই
        print("=== SMTP ERROR while sending OTP ===", flush=True)
        print("Error:", repr(e), flush=True)
        print("Target email:", to_email, flush=True)
        print("OTP:", otp, flush=True)
        print("====================================", flush=True)
        return False, str(e)


def get_user_by_email(email: str):
    """users dict (phone-keyed) থেকে email দিয়ে user বের করি"""
    email = email.lower().strip()
    for u in users.values():
        if u.get("email", "").lower().strip() == email:
            return u
    return None


# ===================== GLOBAL DATA (MAP POINTS) =====================

# সব পয়েন্ট (inside + outside, manual + csv)
points = []

# shapefile থেকে আসা GeoJSON data (global)
shapefile_geojson = None


# ---------- Helper: India boundary check ----------
def is_inside_india(lat, lon):
    """India bounding box: latitude 6 to 38, longitude 68 to 98"""
    return 6 <= lat <= 38 and 68 <= lon <= 98


# ---------- Helper: validation functions ----------
def is_valid_phone(phone: str) -> bool:
    # ঠিক ১০ digit, শুধু সংখ্যা
    return bool(re.fullmatch(r"\d{10}", phone))


def is_valid_email(email: str) -> bool:
    """
    এখন শুধু @gmail.com ইমেলই allow করব
    """
    email = email.lower().strip()
    return email.endswith("@gmail.com")


def is_valid_password(pw: str) -> bool:
    # ঠিক ১০ character, অন্তত ১টা uppercase, ১টা lowercase, ১টা digit
    if len(pw) != 10:
        return False
    if not re.search(r"[A-Z]", pw):
        return False
    if not re.search(r"[a-z]", pw):
        return False
    if not re.search(r"\d", pw):
        return False
    return True


# ===================== AUTH ROUTES =====================

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        error = None

        if not is_valid_phone(phone):
            error = "Phone must be exactly 10 digits."
        elif not is_valid_email(email):
            error = "Email must end with @gmail.com."
        elif not is_valid_password(password):
            error = "Password must be 10 characters with uppercase, lowercase and a number."
        elif phone in users:
            error = "This phone number is already registered."

        if error:
            return render_template("signup.html", error=error, phone=phone, email=email)
        else:
            users[phone] = {
                "phone": phone,
                "email": email,
                "password": password  # demo purpose only
            }
            save_users()
            return redirect(url_for("login"))

    return render_template("signup.html", error=None, phone="", email="")


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    Login = Email (@gmail.com) + OTP
    Step 1: user email submit করবে -> OTP send
    Step 2: OTP verify -> session["user_phone"] সেট করে login
    """
    if request.method == "POST":
        step = request.form.get("step", "send_otp")
        email = request.form.get("email", "").strip()
        otp_input = request.form.get("otp", "").strip()

        if step == "send_otp":
            # প্রথম ধাপ: OTP পাঠানো
            if not is_valid_email(email):
                error = "Please enter a valid email (@gmail.com)."
                return render_template("login.html", error=error, info=None, email=email, otp_sent=False)

            user = get_user_by_email(email)
            if not user:
                error = "No account found with this email."
                return render_template("login.html", error=error, info=None, email=email, otp_sent=False)

            otp = generate_otp()
            expires_at = datetime.now() + timedelta(minutes=5)
            otp_store[email.lower()] = {
                "otp": otp,
                "expires_at": expires_at,
                "purpose": "login"
            }

            success, smtp_err = send_otp_email(email, otp, purpose="login")

            if success:
                info = "OTP sent to your email address. Please check your inbox (and spam)."
            else:
                # Dev friendly: ইমেল না গেলে পেজেই OTP দেখিয়ে দিচ্ছি
                info = (
                    "Could not send OTP email (SMTP error). "
                    f"For now, use this OTP: {otp}. "
                    "Check console for details."
                )

            return render_template("login.html", error=None, info=info, email=email, otp_sent=True)

        elif step == "verify_otp":
            # দ্বিতীয় ধাপ: OTP verify
            if not is_valid_email(email):
                error = "Invalid email."
                return render_template("login.html", error=error, info=None, email=email, otp_sent=True)

            rec = otp_store.get(email.lower())
            if not rec:
                error = "No OTP found for this email. Please request a new OTP."
                return render_template("login.html", error=error, info=None, email=email, otp_sent=False)

            if datetime.now() > rec["expires_at"]:
                otp_store.pop(email.lower(), None)
                error = "OTP expired. Please request a new OTP."
                return render_template("login.html", error=error, info=None, email=email, otp_sent=False)

            if otp_input != rec["otp"]:
                error = "Incorrect OTP."
                return render_template("login.html", error=error, info=None, email=email, otp_sent=True)

            # OTP OK → login success
            otp_store.pop(email.lower(), None)
            user = get_user_by_email(email)
            if not user:
                error = "User not found for this email."
                return render_template("login.html", error=error, info=None, email=email, otp_sent=False)

            session["user_phone"] = user["phone"]
            return redirect(url_for("index"))

    # GET request → first step (email input)
    return render_template("login.html", error=None, info=None, email="", otp_sent=False)


@app.route("/logout")
def logout():
    session.pop("user_phone", None)
    return redirect(url_for("login"))


@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    """
    Forgot password → Email OTP দিয়ে পাসওয়ার্ড reset
    Step 1: email দিয়ে OTP send
    Step 2: email + OTP + new password → verify & update
    """
    if request.method == "POST":
        step = request.form.get("step", "send_otp")
        email = request.form.get("email", "").strip()
        otp_input = request.form.get("otp", "").strip()
        new_password = request.form.get("password", "")

        if step == "send_otp":
            if not is_valid_email(email):
                error = "Please enter a valid email (@gmail.com)."
                return render_template("forgot_password.html", error=error, info=None, email=email, otp_sent=False)

            user = get_user_by_email(email)
            if not user:
                error = "No account found with this email."
                return render_template("forgot_password.html", error=error, info=None, email=email, otp_sent=False)

            otp = generate_otp()
            expires_at = datetime.now() + timedelta(minutes=5)
            otp_store[email.lower()] = {
                "otp": otp,
                "expires_at": expires_at,
                "purpose": "reset"
            }

            success, smtp_err = send_otp_email(email, otp, purpose="password reset")

            if success:
                info = "Password reset OTP sent to your email."
            else:
                info = (
                    "Could not send password reset OTP email (SMTP error). "
                    f"For now, use this OTP: {otp}. "
                    "Check console for details."
                )

            return render_template("forgot_password.html", error=None, info=info, email=email, otp_sent=True)

        elif step == "reset":
            if not is_valid_email(email):
                error = "Invalid email."
                return render_template("forgot_password.html", error=error, info=None, email=email, otp_sent=True)

            rec = otp_store.get(email.lower())
            if not rec or rec["purpose"] != "reset":
                error = "No reset OTP for this email. Please request a new OTP."
                return render_template("forgot_password.html", error=error, info=None, email=email, otp_sent=False)

            if datetime.now() > rec["expires_at"]:
                otp_store.pop(email.lower(), None)
                error = "OTP expired. Please request a new OTP."
                return render_template("forgot_password.html", error=error, info=None, email=email, otp_sent=False)

            if otp_input != rec["otp"]:
                error = "Incorrect OTP."
                return render_template("forgot_password.html", error=error, info=None, email=email, otp_sent=True)

            if not is_valid_password(new_password):
                error = "Password must be 10 characters with uppercase, lowercase and a number."
                return render_template("forgot_password.html", error=error, info=None, email=email, otp_sent=True)

            user = get_user_by_email(email)
            if not user:
                error = "User not found anymore."
                return render_template("forgot_password.html", error=error, info=None, email=email, otp_sent=False)

            # পাসওয়ার্ড আপডেট
            user["password"] = new_password
            save_users()
            otp_store.pop(email.lower(), None)
            return redirect(url_for("login"))

    return render_template("forgot_password.html", error=None, info=None, email="", otp_sent=False)


# ===================== MAIN DASHBOARD =====================

@app.route("/", methods=["GET", "POST"])
def index():
    global shapefile_geojson

    # auth check
    if "user_phone" not in session:
        return redirect(url_for("login"))

    message = ""
    last_lat = ""
    last_lon = ""

    if request.method == "POST":
        form_type = request.form.get("form_type")

        # ------------------ MANUAL INPUT ------------------
        if form_type == "single":
            lat_str = request.form.get("lat", "").strip()
            lon_str = request.form.get("lon", "").strip()
            last_lat, last_lon = lat_str, lon_str

            if not lat_str or not lon_str:
                message = "Please enter both latitude and longitude."
            else:
                try:
                    lat = float(lat_str)
                    lon = float(lon_str)

                    inside = is_inside_india(lat, lon)

                    points.append({
                        "lat": lat,
                        "lon": lon,
                        "source": "input",
                        "inside": inside,
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })

                    message = "Inside India" if inside else "Outside India (saved)"

                except Exception:
                    message = "Latitude/Longitude must be numeric"

        # ------------------ CSV UPLOAD ------------------
        elif form_type == "csv":
            file = request.files.get("csv_file")
            if not file or file.filename == "":
                message = "Please select a CSV file."
            else:
                try:
                    decoded = file.read().decode("utf-8")
                    rdr = csv.reader(io.StringIO(decoded))

                    total = 0
                    added = 0
                    outside_count = 0

                    for row in rdr:
                        if len(row) < 2:
                            continue
                        try:
                            lat = float(row[0])
                            lon = float(row[1])
                        except Exception:
                            continue

                        total += 1
                        inside = is_inside_india(lat, lon)

                        points.append({
                            "lat": lat,
                            "lon": lon,
                            "source": "csv",
                            "inside": inside,
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                        added += 1
                        if not inside:
                            outside_count += 1

                    message = f"CSV rows: {total}, added: {added}, outside India: {outside_count}"

                except Exception as e:
                    print("CSV Error:", e)
                    message = "CSV file read error."

        # ------------------ SHAPEFILE UPLOAD (ZIP) ------------------
        elif form_type == "shapefile":
            file = request.files.get("shapefile")
            if not file or file.filename == "":
                message = "Please select a shapefile ZIP."
            else:
                tmp_path = None
                try:
                    # shapefile ZIP টেম্প ফাইলে সেভ করি
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
                    tmp_path = tmp.name
                    file.save(tmp_path)
                    tmp.close()

                    # GeoPandas দিয়ে shapefile পড়ি
                    gdf = gpd.read_file(tmp_path)

                    # GeoJSON এ convert
                    shapefile_geojson = json.loads(gdf.to_json())
                    message = f"Shapefile loaded successfully. Features: {len(gdf)}"

                except Exception as e:
                    print("Shapefile Error:", e)
                    shapefile_geojson = None
                    message = "Error reading shapefile. Make sure it's a valid ZIP."
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        os.remove(tmp_path)

    # outside India points
    outside_points = [p for p in points if not p["inside"]]
    points_json = json.dumps(points)
    shapefile_json = json.dumps(shapefile_geojson) if shapefile_geojson is not None else "null"

    # phone number mask
    phone = session.get("user_phone")
    if phone and len(phone) >= 5:
        masked_phone = phone[:5] + "XXXXX"
    else:
        masked_phone = "XXXXX"

    return render_template(
        "index.html",
        message=message,
        last_lat=last_lat,
        last_lon=last_lon,
        points=points,
        outside_points=outside_points,
        points_json=points_json,
        shapefile_json=shapefile_json,
        user_phone=phone,
        masked_phone=masked_phone
    )


# 👉 সব পয়েন্টের (inside + outside) CSV
@app.route("/download_all_csv")
def download_all_csv():
    if "user_phone" not in session:
        return redirect(url_for("login"))

    out = io.StringIO()
    wr = csv.writer(out)
    wr.writerow(["Index", "Latitude", "Longitude", "InsideIndia", "Created", "Source"])

    for i, p in enumerate(points, start=1):
        wr.writerow([
            i,
            p["lat"],
            p["lon"],
            "Yes" if p["inside"] else "No",
            p["created_at"],
            p["source"]
        ])

    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=all_points_india.csv"}
    )


# 👉 শুধু OUTSIDE India পয়েন্টের CSV
@app.route("/download_wrong_csv")
def download_wrong_csv():
    if "user_phone" not in session:
        return redirect(url_for("login"))

    outside = [p for p in points if not p["inside"]]

    out = io.StringIO()
    wr = csv.writer(out)
    wr.writerow(["Index", "Latitude", "Longitude", "Created", "Source"])

    for i, p in enumerate(outside, start=1):
        wr.writerow([i, p["lat"], p["lon"], p["created_at"], p["source"]])

    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=outside_india_points.csv"}
    )


# ===================== WEATHER API ROUTE (Open-Meteo, NO KEY) =====================

WEATHER_CODE_MAP = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Fog (rime)",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    80: "Rain showers",
    81: "Rain showers",
    82: "Heavy rain showers",
    95: "Thunderstorm",
    96: "Thunderstorm (hail)",
    99: "Thunderstorm (heavy hail)",
}


@app.route("/api/weather")
def api_weather():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current_weather": "true",
            "timezone": "auto",
        }
        resp = requests.get(url, params=params, timeout=5)
        print("Open-Meteo status:", resp.status_code)

        if resp.status_code != 200:
            return jsonify({
                "error": "Weather API error",
                "status_code": resp.status_code
            }), resp.status_code

        data = resp.json()
        current = data.get("current_weather")
        if not current:
            return jsonify({"error": "No current weather data."}), 500

        temp_c = current.get("temperature")
        wind_speed = current.get("windspeed")
        code = current.get("weathercode")
        desc = WEATHER_CODE_MAP.get(code, f"Weather code {code}")

        result = {
            "name": "",
            "temp_c": temp_c,
            "humidity": None,
            "wind_speed": wind_speed,
            "description": desc,

        }
        return jsonify(result)

    except Exception as e:
        print("Weather API error (exception):", e)
        return jsonify({"error": "Failed to fetch weather."}), 500


# ===================== BUFFER এর মধ্যে POI CSV DOWNLOAD (OpenStreetMap) =====================

@app.route("/download_buffer_pois")
def download_buffer_pois():

    if "user_phone" not in session:
        return redirect(url_for("login"))

    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    radius_km = request.args.get("radius_km", type=float)

    if not all([lat, lon, radius_km]):
        return "Missing lat / lon / radius_km", 400

    if radius_km <= 0 or radius_km > 100:
        return "radius_km must be between 0 and 100", 400

    radius_m = int(radius_km * 1000)

    overpass_query = f"""
    [out:json][timeout:60];
    (
      node(around:{radius_m},{lat},{lon})["amenity"];
      way(around:{radius_m},{lat},{lon})["amenity"];

      node(around:{radius_m},{lat},{lon})["shop"];
      way(around:{radius_m},{lat},{lon})["shop"];

      node(around:{radius_m},{lat},{lon})["highway"="bus_stop"];
      way(around:{radius_m},{lat},{lon})["highway"];

      node(around:{radius_m},{lat},{lon})["railway"];
      way(around:{radius_m},{lat},{lon})["railway"];

      node(around:{radius_m},{lat},{lon})["aeroway"];
      way(around:{radius_m},{lat},{lon})["aeroway"];

      node(around:{radius_m},{lat},{lon})["leisure"="playground"];
      way(around:{radius_m},{lat},{lon})["leisure"="playground"];

      way(around:{radius_m},{lat},{lon})["waterway"];
      way(around:{radius_m},{lat},{lon})["natural"="water"];
    );
    out center;
    """

    try:
        url = "https://overpass-api.de/api/interpreter"

        resp = requests.post(url, data=overpass_query, timeout=120)

        print("Overpass status:", resp.status_code)

        if resp.status_code != 200:
            return "Overpass API failed", 502

        data = resp.json()
        elements = data.get("elements", [])

        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "osm_id","osm_type","category",
            "name","latitude","longitude","tags"
        ])

        for el in elements:

            tags = el.get("tags", {})

            # lat lon extract
            lat_el = el.get("lat")
            lon_el = el.get("lon")

            if not lat_el or not lon_el:
                center = el.get("center", {})
                lat_el = center.get("lat")
                lon_el = center.get("lon")

            if not lat_el or not lon_el:
                continue

            # category detect
            category = "other"
            for k in ["amenity","shop","highway",
                      "railway","aeroway",
                      "leisure","waterway","natural"]:
                if k in tags:
                    category = f"{k}:{tags[k]}"
                    break

            writer.writerow([
                el.get("id"),
                el.get("type"),
                category,
                tags.get("name",""),
                lat_el,
                lon_el,
                json.dumps(tags, ensure_ascii=False)
            ])

        filename = f"buffer_pois_{lat}_{lon}_{radius_km}km.csv"

        return Response(
            output.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition":
                f"attachment; filename={filename}"
            }
        )

    except Exception as e:
        print("ERROR:", e)
        return "Failed to download POI data", 500


if __name__ == "__main__":
    # pip install flask geopandas requests
    app.run(debug=True)
