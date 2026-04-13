from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, jsonify)
import mysql.connector, bcrypt, re, uuid, os, string, random
from functools import wraps
from datetime import datetime, date, timedelta

# ─────────────────────────────────────────────────────────────
#  APP CONFIG
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = 'tickit_very_secret_key_2025_change_in_prod'

# ─────────────────────────────────────────────────────────────
#  DATABASE CONFIG
# ─────────────────────────────────────────────────────────────
DB_CONFIG = {
    'host':     'localhost',
    'user':     'root',
    'password': '',          # ← your MySQL password
    'database': 'tickit_db',
    'charset':  'utf8mb4',
}

def get_db():
    return mysql.connector.connect(**DB_CONFIG)

def query(db, sql, params=(), one=False):
    cur = db.cursor(dictionary=True)
    cur.execute(sql, params)
    return cur.fetchone() if one else cur.fetchall()

def execute(db, sql, params=()):
    cur = db.cursor()
    cur.execute(sql, params)
    return cur.lastrowid

# ─────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────
TICKET_PRICES  = {'Regular': 450, 'Student': 350, 'Senior / PWD': 360}
ADMIN_EMAIL    = 'admin@gmail.com'
ADMIN_PASSWORD = 'admin12345'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

# Payment simulation weights  (must sum to 1.0)
PAY_SUCCESS_RATE = 0.80
PAY_FAILED_RATE  = 0.15
# PAY_PENDING_RATE = 0.05  (remainder)

RESERVATION_MINUTES = 15   # seat lock duration

def allowed_file(f):
    return '.' in f and f.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ─────────────────────────────────────────────────────────────
#  VALIDATORS
# ─────────────────────────────────────────────────────────────
def is_valid_email(v): return bool(re.match(r'^[\w\.-]+@[\w\.-]+\.\w{2,}$', v))
def is_valid_phone(v): return bool(re.match(r'^(\+63|0)\d{10}$', v))

# ─────────────────────────────────────────────────────────────
#  AUTH DECORATORS
# ─────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Admins can browse the main site freely
        if session.get('is_admin'):
            return f(*args, **kwargs)
        if 'user_id' not in session:
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        try:
            db = get_db()
            exists = query(db, "SELECT id FROM users WHERE id=%s",
                           (session['user_id'],), one=True)
            db.close()
            if not exists:
                session.clear()
                flash('Session expired. Please log in again.', 'warning')
                return redirect(url_for('login'))
        except Exception:
            pass
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            flash('Admin access required.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ─────────────────────────────────────────────────────────────
#  DB / FORMATTING HELPERS
# ─────────────────────────────────────────────────────────────
def _fmt_time(t):
    if not t: return ''
    if isinstance(t, timedelta):
        total = int(t.total_seconds())
        hrs, rem = divmod(total, 3600)
        mins = rem // 60
    else:
        parts = str(t).split(':')
        hrs  = int(parts[0])
        mins = int(parts[1]) if len(parts) > 1 else 0
    suffix = 'AM' if hrs < 12 else 'PM'
    return f'{hrs % 12 or 12}:{mins:02d} {suffix}'

def run_maintenance(db):
    """Auto-complete past showings and release expired seat locks."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    execute(db, """
        UPDATE showings SET status='completed'
         WHERE status IN ('open','scheduled','full')
           AND CONCAT(show_date,' ',show_time) < %s
    """, (now,))
    execute(db, """
        UPDATE seats SET status='available', locked_until=NULL
         WHERE status='locked' AND locked_until < %s
    """, (now,))
    execute(db, """
        UPDATE bookings SET status='Completed'
         WHERE status='Confirmed'
           AND showing_id IN (SELECT id FROM showings WHERE status='completed')
    """)
    # Release expired pending-payment bookings (after 20 min)
    cutoff = (datetime.now() - timedelta(minutes=20)).strftime('%Y-%m-%d %H:%M:%S')
    execute(db, """
        UPDATE seats s
          JOIN bookings b ON b.seat_id=s.id
        SET s.status='available', s.locked_until=NULL
        WHERE b.payment_status='pending'
          AND b.created_at < %s
          AND s.status='locked'
    """, (cutoff,))
    execute(db, """
        UPDATE bookings SET status='Cancelled'
         WHERE payment_status='pending'
           AND created_at < %s
           AND status='Confirmed'
    """, (cutoff,))
    db.commit()

# ─────────────────────────────────────────────────────────────
#  SEAT SEEDING
# ─────────────────────────────────────────────────────────────
def seed_seats_default(db, showing_id):
    """Fallback: hardcoded 5-row layout if no hall config exists."""
    rows_config = [
        ('A', 'VIP'), ('B', 'VIP'),
        ('C', 'Standard'), ('D', 'Standard'), ('E', 'Standard'),
    ]
    for row_label, category in rows_config:
        for num in range(1, 11):
            execute(db, """
                INSERT IGNORE INTO seats
                    (showing_id, row_label, seat_number, seat_code, category, status)
                VALUES (%s,%s,%s,%s,%s,'available')
            """, (showing_id, row_label, num, f"{row_label}{num}", category))
    db.commit()

def seed_seats_from_hall(db, showing_id, hall_id):
    """Generate seats from a hall's admin-configured layout."""
    seat_configs = query(db, """
        SELECT * FROM hall_seat_config
        WHERE hall_id=%s AND is_active=1
        ORDER BY row_label, col_number
    """, (hall_id,))

    if not seat_configs:
        seed_seats_default(db, showing_id)
        return

    total = 0
    for sc in seat_configs:
        category = 'VIP' if sc['seat_type'] == 'VIP' else 'Standard'
        execute(db, """
            INSERT IGNORE INTO seats
                (showing_id, row_label, seat_number, seat_code, category, status)
            VALUES (%s,%s,%s,%s,%s,'available')
        """, (showing_id, sc['row_label'], sc['col_number'], sc['seat_code'], category))
        total += 1

    if total:
        execute(db, "UPDATE showings SET total_seats=%s WHERE id=%s", (total, showing_id))
    db.commit()

def ensure_seats(db, showing_id):
    """Seed seats for a showing if none exist yet."""
    row = query(db, "SELECT COUNT(*) AS cnt FROM seats WHERE showing_id=%s",
                (showing_id,), one=True)
    if row['cnt'] > 0:
        return

    showing = query(db, "SELECT cinema_id, hall_id FROM showings WHERE id=%s",
                    (showing_id,), one=True)
    if not showing:
        return

    hall_id = showing.get('hall_id')
    if not hall_id:
        # Auto-pick first configured hall for this cinema
        hall = query(db,
            "SELECT id FROM cinema_halls WHERE cinema_id=%s ORDER BY id LIMIT 1",
            (showing['cinema_id'],), one=True)
        if hall:
            hall_id = hall['id']

    if hall_id:
        seed_seats_from_hall(db, showing_id, hall_id)
    else:
        seed_seats_default(db, showing_id)

def ensure_future_showings(db, movie_id, cinema_id, days_ahead=3):
    today = date.today().isoformat()
    limit = (date.today() + timedelta(days=days_ahead)).isoformat()
    row = query(db, """
        SELECT COUNT(*) AS cnt FROM showings
         WHERE movie_id=%s AND cinema_id=%s
           AND show_date > %s AND show_date <= %s
           AND status IN ('open','scheduled')
    """, (movie_id, cinema_id, today, limit), one=True)

    if row['cnt'] < 2:
        timeslots = ['10:00:00', '13:30:00', '16:30:00', '19:30:00', '22:00:00']
        for d in range(0, days_ahead + 1):
            show_date = (date.today() + timedelta(days=d)).isoformat()
            for t in timeslots:
                execute(db, """
                    INSERT IGNORE INTO showings
                        (movie_id, cinema_id, show_date, show_time, status)
                    VALUES (%s,%s,%s,%s,'open')
                """, (movie_id, cinema_id, show_date, t))
        db.commit()

def get_movies_with_status(db):
    now   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today = date.today().isoformat()
    raw = query(db, """
        SELECT m.id, m.title, m.genre, m.rating, m.poster_path, m.duration_mins,
               (SELECT MIN(s.show_date) FROM showings s
                 WHERE s.movie_id=m.id AND s.status IN ('open','scheduled')
                   AND CONCAT(s.show_date,' ',s.show_time) > %s
               ) AS next_date,
               (SELECT COUNT(*) FROM showings s
                 WHERE s.movie_id=m.id AND s.show_date=%s
                   AND s.status IN ('open','full')) AS today_count,
               (SELECT MAX(s.show_date) FROM showings s
                 WHERE s.movie_id=m.id AND s.status='completed') AS last_played
        FROM movies m WHERE m.status='active'
        ORDER BY today_count DESC, next_date ASC
    """, (now, today))
    return [dict(r) for r in raw]

# ─────────────────────────────────────────────────────────────
#  PUBLIC ROUTES
# ─────────────────────────────────────────────────────────────
@app.route('/')
def landing():
    if 'user_id' in session:
        return redirect(url_for('index'))
    return render_template('landing.html')

@app.route('/home')
@login_required
def index():
    db = get_db()
    run_maintenance(db)
    movies = get_movies_with_status(db)
    db.close()
    return render_template('index.html',
                           user_name=session.get('user_name') or session.get('admin_name', 'Admin'), movies=movies)

@app.route('/movies')
@login_required
def movies():
    db = get_db()
    run_maintenance(db)
    movies_list = get_movies_with_status(db)
    db.close()
    return render_template('movies.html',
                           user_name=session.get('user_name') or session.get('admin_name', 'Admin'), movies=movies_list)

# ─────────────────────────────────────────────────────────────
#  BOOKING FLOW
# ─────────────────────────────────────────────────────────────
@app.route('/booking')
@login_required
def booking():
    db = get_db()
    run_maintenance(db)

    movie_id   = request.args.get('movie_id',   type=int)
    showing_id = request.args.get('showing_id', type=int)

    today = date.today().isoformat()
    limit = (date.today() + timedelta(days=3)).isoformat()
    now   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    all_movies = get_movies_with_status(db)

    selected_movie   = None
    showings_by_date = {}
    selected_showing = None
    seat_rows        = []

    if movie_id:
        selected_movie = query(db,
            "SELECT * FROM movies WHERE id=%s AND status='active'",
            (movie_id,), one=True)

        if selected_movie:
            cinemas_all = query(db, "SELECT id FROM cinemas")
            for _c in cinemas_all:
                ensure_future_showings(db, movie_id, _c['id'], days_ahead=3)

            raw_showings = query(db, """
                SELECT s.id, s.show_date, s.show_time, s.status, s.total_seats,
                       c.name AS cinema_name, c.location AS cinema_location,
                       COALESCE(
                           (SELECT COUNT(*) FROM seats st
                            WHERE st.showing_id=s.id AND st.status='booked'),0
                       ) AS booked_count,
                       COALESCE(
                           (SELECT COUNT(*) FROM seats st
                            WHERE st.showing_id=s.id AND st.status='available'),0
                       ) AS avail_count,
                       COALESCE(
                           (SELECT COUNT(*) FROM seats st
                            WHERE st.showing_id=s.id),0
                       ) AS total_seeded
                FROM showings s
                JOIN cinemas c ON c.id=s.cinema_id
                WHERE s.movie_id=%s
                  AND s.status IN ('open','scheduled','full')
                  AND CONCAT(s.show_date,' ',s.show_time) > %s
                  AND s.show_date <= %s
                ORDER BY s.show_date, s.show_time
            """, (movie_id, now, limit))

            for sh in raw_showings:
                sh = dict(sh)
                if sh['total_seeded'] == 0:
                    ensure_seats(db, sh['id'])
                    sh['avail_count'] = 50
                if sh['avail_count'] == 0 and sh['booked_count'] == 0:
                    sh['avail_count'] = sh['total_seats']

                d_obj   = sh['show_date']
                d_str   = d_obj.isoformat() if hasattr(d_obj, 'isoformat') else str(d_obj)
                d_label = d_obj.strftime('%A, %B %d %Y') if hasattr(d_obj, 'strftime') else d_str

                if d_str not in showings_by_date:
                    showings_by_date[d_str] = {'label': d_label, 'showings': []}

                avail = sh['avail_count']
                if avail == 0:
                    sh['avail_label'] = 'SOLD OUT'
                    sh['avail_class'] = 'full'
                elif avail <= 8:
                    sh['avail_label'] = f'Only {avail} left!'
                    sh['avail_class'] = 'low'
                else:
                    sh['avail_label'] = f'{avail} of {sh["total_seats"]} available'
                    sh['avail_class'] = 'ok'

                sh['show_time_fmt'] = _fmt_time(sh['show_time'])
                showings_by_date[d_str]['showings'].append(sh)

    if showing_id:
        ensure_seats(db, showing_id)
        row = query(db, """
            SELECT s.id, s.show_date, s.show_time, s.status AS show_status,
                   s.total_seats,
                   c.name AS cinema_name, c.location AS cinema_location,
                   m.title AS movie_title, m.genre, m.rating, m.poster_path,
                   m.id AS movie_id_val
            FROM showings s
            JOIN cinemas c ON c.id=s.cinema_id
            JOIN movies  m ON m.id=s.movie_id
            WHERE s.id=%s
        """, (showing_id,), one=True)

        if row:
            selected_showing = dict(row)
            selected_showing['show_time_fmt'] = _fmt_time(selected_showing['show_time'])
            d_obj = selected_showing['show_date']
            selected_showing['show_date_fmt'] = (
                d_obj.strftime('%A, %B %d %Y') if hasattr(d_obj, 'strftime') else str(d_obj))
            if not movie_id:
                movie_id = selected_showing['movie_id_val']
            if not selected_movie:
                selected_movie = {
                    'id':          selected_showing['movie_id_val'],
                    'title':       selected_showing['movie_title'],
                    'genre':       selected_showing['genre'],
                    'rating':      selected_showing['rating'],
                    'poster_path': selected_showing['poster_path'],
                }

        all_seats_raw = query(db, """
            SELECT st.id, st.row_label, st.seat_number, st.seat_code,
                   st.category, st.status, st.locked_until
            FROM seats st
            WHERE st.showing_id=%s
            ORDER BY st.row_label, st.seat_number
        """, (showing_id,))

        from collections import defaultdict
        rows_dict = defaultdict(list)
        for s in all_seats_raw:
            rows_dict[s['row_label']].append(dict(s))
        seat_rows = [{'label': k, 'seats': v, 'category': v[0]['category']}
                     for k, v in sorted(rows_dict.items())]

    db.close()
    return render_template('booking.html',
        user_name        = session.get('user_name'),
        all_movies       = all_movies,
        selected_movie   = selected_movie,
        movie_id         = movie_id,
        showings_by_date = showings_by_date,
        selected_showing = selected_showing,
        showing_id       = showing_id,
        seat_rows        = seat_rows,
        booking_success  = False,
        errors={}, form={},
        ticket_prices    = TICKET_PRICES,
    )

# ─────────────────────────────────────────────────────────────
#  SEAT API  (real-time lock / unlock / status)
# ─────────────────────────────────────────────────────────────
@app.route('/api/lock-seat', methods=['POST'])
@login_required
def lock_seat():
    data       = request.get_json(force=True)
    seat_id    = data.get('seat_id')
    showing_id = data.get('showing_id')
    if not seat_id or not showing_id:
        return jsonify({'ok': False, 'msg': 'Missing params'})

    db  = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        # Release expired locks first
        execute(db, """
            UPDATE seats SET status='available', locked_until=NULL
             WHERE showing_id=%s AND status='locked' AND locked_until < %s
        """, (showing_id, now))

        seat = query(db, "SELECT * FROM seats WHERE id=%s", (seat_id,), one=True)
        if not seat or seat['status'] != 'available':
            db.close()
            return jsonify({'ok': False, 'msg': 'Seat no longer available'})

        lock_exp = (datetime.now() + timedelta(minutes=RESERVATION_MINUTES)
                    ).strftime('%Y-%m-%d %H:%M:%S')
        execute(db, "UPDATE seats SET status='locked', locked_until=%s WHERE id=%s",
                (lock_exp, seat_id))
        db.commit()
        db.close()
        return jsonify({'ok': True, 'expires': lock_exp})
    except Exception as e:
        db.close()
        return jsonify({'ok': False, 'msg': str(e)})

@app.route('/api/unlock-seat', methods=['POST'])
@login_required
def unlock_seat():
    data    = request.get_json(force=True)
    seat_id = data.get('seat_id')
    if not seat_id:
        return jsonify({'ok': False})
    db = get_db()
    execute(db,
        "UPDATE seats SET status='available', locked_until=NULL WHERE id=%s AND status='locked'",
        (seat_id,))
    db.commit(); db.close()
    return jsonify({'ok': True})

@app.route('/api/seat-status/<int:showing_id>')
@login_required
def seat_status(showing_id):
    db  = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    execute(db, """
        UPDATE seats SET status='available', locked_until=NULL
         WHERE showing_id=%s AND status='locked' AND locked_until < %s
    """, (showing_id, now))
    db.commit()
    seats = query(db, """
        SELECT id, seat_code, status, category, row_label, seat_number
        FROM seats WHERE showing_id=%s ORDER BY row_label, seat_number
    """, (showing_id,))
    db.close()
    return jsonify({'seats': seats})

# ─────────────────────────────────────────────────────────────
#  CONFIRM BOOKING  →  redirect to Fake Payment Page
# ─────────────────────────────────────────────────────────────
@app.route('/booking/confirm', methods=['POST'])
@login_required
def confirm_booking():
    seat_ids_raw  = request.form.get('seat_ids', '').strip()
    showing_id    = request.form.get('showing_id', type=int)
    ticket_type   = request.form.get('ticket_type', 'Regular')
    customer_name = request.form.get('customer_name', '').strip()
    contact       = request.form.get('contact', '').strip()
    special       = request.form.get('special_requests', '').strip()

    errors = {}
    if not seat_ids_raw:
        errors['seats'] = 'Please select at least one seat.'
    if not showing_id:
        errors['showing'] = 'Invalid showing.'
    if not customer_name or len(customer_name) < 2:
        errors['customer_name'] = 'Valid name required (min 2 chars).'
    if not contact or not re.match(r'^(\+63|0)\d{10}$', contact):
        errors['contact'] = 'Enter a valid PH mobile (09XXXXXXXXX).'
    if ticket_type not in TICKET_PRICES:
        errors['ticket_type'] = 'Invalid ticket type.'

    seat_ids = [int(x) for x in seat_ids_raw.split(',') if x.strip().isdigit()]
    if not seat_ids:
        errors['seats'] = 'No valid seats selected.'
    elif len(seat_ids) > 10:
        errors['seats'] = 'Maximum 10 seats per booking.'

    if errors:
        flash(' | '.join(errors.values()), 'error')
        return redirect(url_for('booking', showing_id=showing_id))

    db = get_db()
    try:
        showing = query(db, "SELECT * FROM showings WHERE id=%s", (showing_id,), one=True)
        if not showing or showing['status'] not in ('open', 'scheduled', 'full'):
            flash('This showing is no longer available.', 'error')
            db.close(); return redirect(url_for('booking'))

        # Verify seats are still available or locked by this user
        for sid in seat_ids:
            seat = query(db, "SELECT * FROM seats WHERE id=%s", (sid,), one=True)
            if not seat or seat['status'] == 'booked':
                code = seat['seat_code'] if seat else str(sid)
                flash(f'Seat {code} was just taken. Please re-select.', 'error')
                db.close()
                return redirect(url_for('booking', showing_id=showing_id))

        unit_price  = TICKET_PRICES[ticket_type]
        total_price = unit_price * len(seat_ids)
        ref_code    = 'TKT-' + uuid.uuid4().hex[:8].upper()

        discount_status = 'none'
        if ticket_type in ('Student', 'Senior / PWD'):
            discount_status = 'pending_verification'

        placeholders = ','.join(['%s'] * len(seat_ids))
        seat_info = query(db,
            f"SELECT seat_code, category FROM seats WHERE id IN ({placeholders})", seat_ids)
        seat_codes_str = ', '.join(f"{s['seat_code']} ({s['category']})" for s in seat_info)

        sh_info = query(db, """
            SELECT m.title, c.name AS cinema, s.show_date, s.show_time
            FROM showings s
            JOIN movies  m ON m.id=s.movie_id
            JOIN cinemas c ON c.id=s.cinema_id
            WHERE s.id=%s
        """, (showing_id,), one=True)

        # ── Lock seats + create PENDING booking records ─────────
        lock_exp = (datetime.now() + timedelta(minutes=RESERVATION_MINUTES)
                    ).strftime('%Y-%m-%d %H:%M:%S')

        for sid in seat_ids:
            execute(db, "UPDATE seats SET status='locked', locked_until=%s WHERE id=%s",
                    (lock_exp, sid))
            execute(db, """
                INSERT INTO bookings
                    (user_id, showing_id, seat_id, booking_ref, ref_code,
                     ticket_type, ticket_count, unit_price, total_price,
                     seat_codes, customer_name, contact, special_requests,
                     discount_status, payment_status, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending','Confirmed')
            """, (session['user_id'], showing_id, sid, ref_code, ref_code,
                  ticket_type, len(seat_ids), unit_price, total_price,
                  seat_codes_str, customer_name, contact, special, discount_status))

        db.commit()
        db.close()

        # Redirect to fake payment page
        return redirect(url_for('payment_checkout', ref=ref_code))

    except Exception as e:
        try: db.close()
        except: pass
        flash(f'Booking error: {str(e)}', 'error')
        return redirect(url_for('booking', showing_id=showing_id))

# ─────────────────────────────────────────────────────────────
#  FAKE PAYMENT CHECKOUT  (local simulation, no real API)
# ─────────────────────────────────────────────────────────────
@app.route('/payment/checkout')
@login_required
def payment_checkout():
    ref_code = request.args.get('ref', '').strip()
    if not ref_code:
        flash('Invalid booking reference.', 'error')
        return redirect(url_for('booking'))

    db = get_db()
    booking = query(db, """
        SELECT b.ref_code, b.customer_name, b.ticket_type,
               b.total_price, b.ticket_count, b.seat_codes,
               b.payment_status, b.discount_status, b.created_at,
               m.title AS movie, m.poster_path,
               c.name AS cinema,
               s.show_date, s.show_time
        FROM bookings b
        JOIN showings s ON s.id=b.showing_id
        JOIN movies   m ON m.id=s.movie_id
        JOIN cinemas  c ON c.id=s.cinema_id
        WHERE b.ref_code=%s AND b.user_id=%s
        LIMIT 1
    """, (ref_code, session['user_id']), one=True)
    db.close()

    if not booking:
        flash('Booking not found or already processed.', 'error')
        return redirect(url_for('booking'))

    # If already paid, go straight to result
    if booking['payment_status'] == 'paid':
        return redirect(url_for('payment_result', ref=ref_code))

    booking = dict(booking)
    d_obj = booking['show_date']
    booking['date_fmt'] = d_obj.strftime('%a, %b %d %Y') if hasattr(d_obj, 'strftime') else str(d_obj)
    booking['time_fmt'] = _fmt_time(booking['show_time'])

    # Calculate how many minutes left on reservation
    created = booking['created_at']
    elapsed = (datetime.now() - created).total_seconds() if created else 0
    secs_left = max(0, int(RESERVATION_MINUTES * 60 - elapsed))

    return render_template('payment_page.html',
                           user_name=session.get('user_name') or session.get('admin_name', 'Admin'),
                           booking=booking,
                           secs_left=secs_left)

@app.route('/payment/process', methods=['POST'])
@login_required
def payment_process():
    """
    Simulates PayMongo payment processing locally.
    No real API calls — purely randomised result with realistic UX.
    """
    ref_code = request.form.get('ref_code', '').strip()
    method   = request.form.get('payment_method', 'credit_card')

    if not ref_code:
        return jsonify({'ok': False, 'msg': 'Invalid reference.', 'status': 'error'})

    db = get_db()
    try:
        bookings_list = query(db, """
            SELECT b.id, b.seat_id, b.showing_id, b.total_price, b.payment_status
            FROM bookings b
            WHERE b.ref_code=%s AND b.user_id=%s
        """, (ref_code, session['user_id']))

        if not bookings_list:
            db.close()
            return jsonify({'ok': False, 'msg': 'Booking not found.', 'status': 'error'})

        if bookings_list[0]['payment_status'] == 'paid':
            db.close()
            return jsonify({'ok': True, 'status': 'paid', 'msg': 'Payment already processed!'})

        # ── Simulate payment gateway delay + result ───────────
        roll = random.random()
        if roll < PAY_SUCCESS_RATE:
            pay_status = 'paid'
        elif roll < PAY_SUCCESS_RATE + PAY_FAILED_RATE:
            pay_status = 'failed'
        else:
            pay_status = 'pending'

        fake_id = 'SIM-' + uuid.uuid4().hex[:14].upper()
        amount  = bookings_list[0]['total_price']

        if pay_status == 'paid':
            # Mark all seats as booked
            for b in bookings_list:
                execute(db, "UPDATE seats SET status='booked', locked_until=NULL WHERE id=%s",
                        (b['seat_id'],))
            execute(db,
                "UPDATE bookings SET payment_status='paid', status='Confirmed' WHERE ref_code=%s",
                (ref_code,))
            execute(db, """
                INSERT INTO payments
                    (booking_ref, user_id, amount, payment_method, paymongo_link_id, status, paid_at)
                VALUES (%s,%s,%s,%s,%s,'paid',NOW())
            """, (ref_code, session['user_id'], amount, method, fake_id))

            # Check if showing is now full
            avail = query(db,
                "SELECT COUNT(*) AS cnt FROM seats WHERE showing_id=%s AND status='available'",
                (bookings_list[0]['showing_id'],), one=True)['cnt']
            if avail == 0:
                execute(db, "UPDATE showings SET status='full' WHERE id=%s",
                        (bookings_list[0]['showing_id'],))

        elif pay_status == 'failed':
            # Release all locked seats
            for b in bookings_list:
                execute(db,
                    "UPDATE seats SET status='available', locked_until=NULL WHERE id=%s",
                    (b['seat_id'],))
            execute(db,
                "UPDATE bookings SET payment_status='failed', status='Cancelled' WHERE ref_code=%s",
                (ref_code,))
            execute(db, """
                INSERT INTO payments
                    (booking_ref, user_id, amount, payment_method, paymongo_link_id, status, failed_at)
                VALUES (%s,%s,%s,%s,%s,'failed',NOW())
            """, (ref_code, session['user_id'], amount, method, fake_id))

        else:  # pending
            execute(db, """
                INSERT INTO payments
                    (booking_ref, user_id, amount, payment_method, paymongo_link_id, status)
                VALUES (%s,%s,%s,%s,%s,'pending')
            """, (ref_code, session['user_id'], amount, method, fake_id))

        db.commit()
        db.close()

        msgs = {
            'paid':    'Payment successful! Your booking is confirmed. 🎉',
            'failed':  'Payment declined. Your seats have been released.',
            'pending': 'Payment is processing. We\'ll confirm shortly.',
        }
        return jsonify({
            'ok':        pay_status != 'error',
            'status':    pay_status,
            'payment_id': fake_id,
            'msg':       msgs[pay_status],
        })

    except Exception as e:
        try: db.close()
        except: pass
        return jsonify({'ok': False, 'msg': str(e), 'status': 'error'})

@app.route('/payment/result')
@login_required
def payment_result():
    ref_code = request.args.get('ref', '').strip()
    db = get_db()
    booking_data = None
    pay_row = None

    if ref_code:
        row = query(db, """
            SELECT b.ref_code, b.ticket_type, b.total_price, b.ticket_count,
                   b.seat_codes, b.customer_name, b.status, b.payment_status,
                   b.discount_status,
                   m.title AS movie, c.name AS cinema,
                   s.show_date, s.show_time
            FROM bookings b
            JOIN showings s ON s.id=b.showing_id
            JOIN movies   m ON m.id=s.movie_id
            JOIN cinemas  c ON c.id=s.cinema_id
            WHERE b.ref_code=%s AND b.user_id=%s
            LIMIT 1
        """, (ref_code, session['user_id']), one=True)

        pay_row = query(db,
            "SELECT * FROM payments WHERE booking_ref=%s ORDER BY id DESC LIMIT 1",
            (ref_code,), one=True)

        db.close()
        if row:
            booking_data = dict(row)
            d_obj = booking_data['show_date']
            booking_data['date_fmt'] = (
                d_obj.strftime('%A, %B %d %Y') if hasattr(d_obj, 'strftime') else str(d_obj))
            booking_data['time_fmt'] = _fmt_time(booking_data['show_time'])
    else:
        db.close()

    payment_status = booking_data['payment_status'] if booking_data else 'unknown'
    return render_template('payment_success.html',
                           user_name=session.get('user_name') or session.get('admin_name', 'Admin'),
                           booking=booking_data,
                           payment_status=payment_status,
                           pay_row=pay_row)

# Keep old /payment/success route as alias for backward compat
@app.route('/payment/success')
@login_required
def payment_success():
    ref = request.args.get('ref', '')
    return redirect(url_for('payment_result', ref=ref))

@app.route('/payment/cancel')
@login_required
def payment_cancel():
    ref_code = request.args.get('ref', '')
    if ref_code:
        try:
            db = get_db()
            # Release seats
            seats_to_free = query(db,
                "SELECT seat_id FROM bookings WHERE ref_code=%s", (ref_code,))
            for s in seats_to_free:
                execute(db,
                    "UPDATE seats SET status='available', locked_until=NULL WHERE id=%s",
                    (s['seat_id'],))
            execute(db,
                "UPDATE bookings SET payment_status='failed', status='Cancelled' WHERE ref_code=%s",
                (ref_code,))
            execute(db,
                "INSERT INTO payments (booking_ref, status) VALUES (%s, 'failed')",
                (ref_code,))
            db.commit(); db.close()
        except Exception:
            pass
    flash('Payment cancelled. Your seats have been released.', 'warning')
    return redirect(url_for('booking'))

# ─────────────────────────────────────────────────────────────
#  MY BOOKINGS
# ─────────────────────────────────────────────────────────────
@app.route('/my-bookings')
@login_required
def my_bookings():
    db   = get_db()
    rows = query(db, """
        SELECT b.ref_code, b.ticket_type, b.unit_price, b.status AS booking_status,
               b.created_at, b.customer_name, b.contact,
               b.discount_status, b.payment_status,
               st.seat_code, st.category,
               m.title AS movie, c.name AS cinema,
               s.show_date, s.show_time
        FROM bookings b
        JOIN seats    st ON st.id  = b.seat_id
        JOIN showings s  ON s.id   = b.showing_id
        JOIN movies   m  ON m.id   = s.movie_id
        JOIN cinemas  c  ON c.id   = s.cinema_id
        WHERE b.user_id = %s
        ORDER BY b.created_at DESC
    """, (session['user_id'],))
    db.close()

    from collections import defaultdict
    grouped = defaultdict(list)
    for r in rows:
        grouped[r['ref_code']].append(dict(r))

    bookings_list = []
    for ref, seats in grouped.items():
        first = seats[0]
        total = sum(s['unit_price'] for s in seats)
        d_obj = first['show_date']
        date_fmt = d_obj.strftime('%b %d, %Y') if hasattr(d_obj, 'strftime') else str(d_obj)
        bookings_list.append({
            'ref':             ref,
            'movie':           first['movie'],
            'cinema':          first['cinema'],
            'date':            date_fmt,
            'showtime':        _fmt_time(first['show_time']),
            'seats':           ', '.join(s['seat_code'] for s in seats),
            'ticket_type':     first['ticket_type'],
            'total':           total,
            'status':          first['booking_status'],
            'booked_on':       first['created_at'],
            'discount_status': first['discount_status'],
            'payment_status':  first['payment_status'],
        })

    return render_template('my_bookings.html',
                           user_name=session.get('user_name') or session.get('admin_name', 'Admin'),
                           bookings=bookings_list)

# ─────────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if session.get('is_admin'):
        return redirect(url_for('admin_dashboard'))

    errors = {}; form = {}
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        password   = request.form.get('password',   '').strip()
        form = {'identifier': identifier}

        if not identifier:
            errors['identifier'] = 'Email or mobile is required.'
        elif not is_valid_email(identifier) and not is_valid_phone(identifier):
            errors['identifier'] = 'Enter a valid email or PH mobile (09XXXXXXXXX).'
        if not password:
            errors['password'] = 'Password is required.'
        elif len(password) < 6:
            errors['password'] = 'Min 6 characters.'

        if not errors:
            if identifier == ADMIN_EMAIL and password == ADMIN_PASSWORD:
                session['is_admin']   = True
                session['admin_name'] = 'Admin'
                return redirect(url_for('admin_dashboard'))
            try:
                db   = get_db()
                user = query(db,
                    'SELECT * FROM users WHERE email=%s OR mobile=%s',
                    (identifier, identifier), one=True)
                db.close()
                if user and bcrypt.checkpw(password.encode(), user['password'].encode()):
                    session['user_id']   = user['id']
                    session['user_name'] = user['full_name']
                    flash(f'Welcome back, {user["full_name"]}!', 'success')
                    return redirect(url_for('index'))
                else:
                    errors['general'] = 'Invalid credentials. Please try again.'
            except Exception as e:
                errors['general'] = f'Database error: {e}'

    return render_template('login.html', errors=errors, form=form)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('index'))
    errors = {}; form = {}
    if request.method == 'POST':
        identifier = request.form.get('identifier',       '').strip()
        full_name  = request.form.get('full_name',        '').strip()
        age        = request.form.get('age',              '').strip()
        gender     = request.form.get('gender',           '').strip()
        province   = request.form.get('province',         '').strip()
        city       = request.form.get('city',             '').strip()
        barangay   = request.form.get('barangay',         '').strip()
        password   = request.form.get('password',         '').strip()
        confirm_pw = request.form.get('confirm_password', '').strip()
        form = dict(identifier=identifier, full_name=full_name, age=age,
                    gender=gender, province=province, city=city, barangay=barangay)

        if not identifier:                                   errors['identifier']       = 'Required.'
        elif not is_valid_email(identifier) and not is_valid_phone(identifier):
                                                             errors['identifier']       = 'Enter valid email or 09XXXXXXXXX.'
        if not full_name:                                    errors['full_name']        = 'Required.'
        elif len(full_name) < 2:                             errors['full_name']        = 'Min 2 chars.'
        if not age:                                          errors['age']              = 'Required.'
        elif not age.isdigit() or not (1 <= int(age) <= 120):
                                                             errors['age']              = 'Enter valid age (1-120).'
        if not gender:                                       errors['gender']           = 'Select gender.'
        if not province:                                     errors['province']         = 'Select province.'
        if not city:                                         errors['city']             = 'Select city.'
        if not barangay:                                     errors['barangay']         = 'Select barangay.'
        if not password:                                     errors['password']         = 'Required.'
        elif len(password) < 6:                              errors['password']         = 'Min 6 chars.'
        elif not re.search(r'[A-Za-z]', password) or not re.search(r'\d', password):
                                                             errors['password']         = 'Must contain letters and numbers.'
        if not confirm_pw:                                   errors['confirm_password'] = 'Confirm your password.'
        elif password != confirm_pw:                         errors['confirm_password'] = 'Passwords do not match.'

        if not errors:
            try:
                db     = get_db()
                email  = identifier if is_valid_email(identifier) else None
                mobile = identifier if is_valid_phone(identifier) else None
                exists = query(db,
                    'SELECT id FROM users WHERE email=%s OR mobile=%s',
                    (email, mobile), one=True)
                if exists:
                    errors['identifier'] = 'Already registered. Please log in.'
                else:
                    hashed  = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                    address = f"{barangay}, {city}, {province}"
                    execute(db, """
                        INSERT INTO users
                            (email, mobile, full_name, age, gender, address, password)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """, (email, mobile, full_name, int(age), gender, address, hashed))
                    db.commit(); db.close()
                    flash(f'Welcome, {full_name}! Account created. Please log in.', 'success')
                    return redirect(url_for('login'))
                db.close()
            except Exception as e:
                errors['general'] = f'Database error: {e}'

    return render_template('register.html', errors=errors, form=form)

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('landing'))

# ─────────────────────────────────────────────────────────────
#  ADMIN — DASHBOARD
# ─────────────────────────────────────────────────────────────
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('is_admin'):
        return redirect(url_for('admin_dashboard'))
    error = None
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '').strip()
        if u == ADMIN_EMAIL and p == ADMIN_PASSWORD:
            session['is_admin']   = True
            session['admin_name'] = 'Admin'
            return redirect(url_for('admin_dashboard'))
        else:
            error = 'Invalid admin credentials.'
    return render_template('admin_login.html', error=error)

@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    session.pop('admin_name', None)
    return redirect(url_for('admin_login'))

@app.route('/admin')
@admin_required
def admin_dashboard():
    db = get_db()
    stats = {
        'available_seats':    query(db, "SELECT COUNT(*) AS n FROM seats WHERE status='available'", one=True)['n'],
        'total_sales':        query(db, "SELECT COALESCE(SUM(total_price),0) AS n FROM bookings WHERE status IN ('Confirmed','Completed') AND payment_status='paid'", one=True)['n'],
        'total_bookings':     query(db, "SELECT COUNT(*) AS n FROM bookings", one=True)['n'],
        'confirmed_bookings': query(db, "SELECT COUNT(*) AS n FROM bookings WHERE status='Confirmed' AND payment_status='paid'", one=True)['n'],
        'active_movies':      query(db, "SELECT COUNT(*) AS n FROM movies WHERE status='active'", one=True)['n'],
        'total_movies':       query(db, "SELECT COUNT(*) AS n FROM movies", one=True)['n'],
        'total_users':        query(db, "SELECT COUNT(*) AS n FROM users", one=True)['n'],
        'today_showings':     query(db, "SELECT COUNT(*) AS n FROM showings WHERE show_date=%s AND status IN ('open','full')",
                                   (date.today().isoformat(),), one=True)['n'],
        'pending_discounts':  query(db, "SELECT COUNT(*) AS n FROM bookings WHERE discount_status='pending_verification'", one=True)['n'],
        'pending_payments':   query(db, "SELECT COUNT(*) AS n FROM payments WHERE status='pending'", one=True)['n'],
        'total_halls':        query(db, "SELECT COUNT(*) AS n FROM cinema_halls", one=True)['n'],
    }

    recent_bookings = query(db, """
        SELECT b.id, b.booking_ref, b.customer_name, b.total_price, b.status,
               b.ticket_count, b.ticket_type, b.seat_codes,
               b.payment_status, b.discount_status,
               m.title AS movie_title, s.show_date, s.show_time
        FROM bookings b
        JOIN showings s ON b.showing_id=s.id
        JOIN movies   m ON s.movie_id=m.id
        ORDER BY b.id DESC LIMIT 10
    """)

    active_movies = query(db, """
        SELECT m.*, COALESCE((SELECT COUNT(*) FROM showings sh
                              JOIN seats st ON st.showing_id=sh.id
                             WHERE sh.movie_id=m.id AND st.status='available'),0) AS avail_seats
        FROM movies m WHERE m.status='active' ORDER BY m.title
    """)
    db.close()
    return render_template('admin_dashboard.html',
                           stats=stats,
                           recent_bookings=recent_bookings,
                           active_movies=active_movies)

# ─────────────────────────────────────────────────────────────
#  ADMIN — CINEMA HALLS & SEAT LAYOUT EDITOR
# ─────────────────────────────────────────────────────────────
@app.route('/admin/halls')
@admin_required
def admin_halls():
    db = get_db()
    cinemas_list = query(db, "SELECT * FROM cinemas ORDER BY name")
    halls = query(db, """
        SELECT h.id, h.cinema_id, h.hall_name, h.rows_count, h.cols_count, h.created_at,
               c.name AS cinema_name,
               (SELECT COUNT(*) FROM hall_seat_config hsc WHERE hsc.hall_id=h.id) AS seat_count,
               (SELECT COUNT(*) FROM hall_seat_config hsc WHERE hsc.hall_id=h.id AND hsc.seat_type='VIP') AS vip_count,
               (SELECT COUNT(*) FROM hall_seat_config hsc WHERE hsc.hall_id=h.id AND hsc.seat_type='PWD') AS pwd_count,
               (SELECT COUNT(*) FROM hall_seat_config hsc WHERE hsc.hall_id=h.id AND hsc.is_active=0) AS inactive_count
        FROM cinema_halls h
        JOIN cinemas c ON c.id=h.cinema_id
        ORDER BY c.name, h.hall_name
    """)
    db.close()
    return render_template('admin_halls.html', halls=halls, cinemas=cinemas_list)

@app.route('/admin/halls/add', methods=['POST'])
@admin_required
def admin_halls_add():
    cinema_id  = request.form.get('cinema_id', type=int)
    hall_name  = request.form.get('hall_name', '').strip()
    rows_count = request.form.get('rows_count', type=int) or 8
    cols_count = request.form.get('cols_count', type=int) or 10

    if not cinema_id or not hall_name:
        flash('Cinema and hall name are required.', 'error')
        return redirect(url_for('admin_halls'))

    rows_count = max(2, min(26, rows_count))
    cols_count = max(2, min(30, cols_count))
    row_labels = list(string.ascii_uppercase)

    try:
        db = get_db()
        hall_id = execute(db, """
            INSERT INTO cinema_halls (cinema_id, hall_name, rows_count, cols_count)
            VALUES (%s,%s,%s,%s)
        """, (cinema_id, hall_name, rows_count, cols_count))

        # Auto-seed: rows A-B = VIP, rest = Regular
        for r_idx in range(rows_count):
            rl = row_labels[r_idx]
            seat_type = 'VIP' if r_idx < 2 else 'Regular'
            for col in range(1, cols_count + 1):
                execute(db, """
                    INSERT IGNORE INTO hall_seat_config
                        (hall_id, row_label, col_number, seat_code, seat_type, is_active)
                    VALUES (%s,%s,%s,%s,%s,1)
                """, (hall_id, rl, col, f"{rl}{col}", seat_type))

        db.commit(); db.close()
        flash(f'Hall "{hall_name}" created! Customize the layout below.', 'success')
        return redirect(url_for('admin_seat_editor', hall_id=hall_id))
    except Exception as e:
        flash(f'Error creating hall: {e}', 'error')
        return redirect(url_for('admin_halls'))

@app.route('/admin/halls/<int:hall_id>/editor')
@admin_required
def admin_seat_editor(hall_id):
    db = get_db()
    hall = query(db, """
        SELECT h.*, c.name AS cinema_name
        FROM cinema_halls h
        JOIN cinemas c ON c.id=h.cinema_id
        WHERE h.id=%s
    """, (hall_id,), one=True)

    if not hall:
        flash('Hall not found.', 'error')
        return redirect(url_for('admin_halls'))

    seats = query(db,
        "SELECT * FROM hall_seat_config WHERE hall_id=%s ORDER BY row_label, col_number",
        (hall_id,))
    db.close()

    # Build lookup dict keyed by "row-col"
    seat_map = {f"{s['row_label']}-{s['col_number']}": dict(s) for s in seats}

    row_labels  = list(string.ascii_uppercase[:hall['rows_count']])
    col_numbers = list(range(1, hall['cols_count'] + 1))

    return render_template('admin_seat_editor.html',
                           hall=hall,
                           seat_map=seat_map,
                           row_labels=row_labels,
                           col_numbers=col_numbers)

@app.route('/admin/halls/<int:hall_id>/save-layout', methods=['POST'])
@admin_required
def admin_halls_save_layout(hall_id):
    data = request.get_json(force=True)
    seats_data = data.get('seats', [])

    if not seats_data:
        return jsonify({'ok': False, 'msg': 'No seat data received.'})

    db = get_db()
    try:
        execute(db, "DELETE FROM hall_seat_config WHERE hall_id=%s", (hall_id,))
        for s in seats_data:
            execute(db, """
                INSERT INTO hall_seat_config
                    (hall_id, row_label, col_number, seat_code, seat_type, is_active)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (hall_id, s['row'], int(s['col']), s['code'],
                  s['type'], 1 if s['active'] else 0))
        db.commit(); db.close()
        return jsonify({'ok': True, 'msg': f'Layout saved ({len(seats_data)} seats).'})
    except Exception as e:
        db.close()
        return jsonify({'ok': False, 'msg': str(e)})

@app.route('/admin/halls/delete', methods=['POST'])
@admin_required
def admin_halls_delete():
    hall_id = request.form.get('hall_id', type=int)
    if not hall_id:
        flash('Invalid hall.', 'error')
        return redirect(url_for('admin_halls'))
    try:
        db = get_db()
        execute(db, "DELETE FROM hall_seat_config WHERE hall_id=%s", (hall_id,))
        execute(db, "DELETE FROM cinema_halls WHERE id=%s", (hall_id,))
        db.commit(); db.close()
        flash('Hall deleted.', 'success')
    except Exception as e:
        flash(f'Error deleting hall: {e}', 'error')
    return redirect(url_for('admin_halls'))

# ─────────────────────────────────────────────────────────────
#  ADMIN — MOVIES
# ─────────────────────────────────────────────────────────────
@app.route('/admin/movies')
@admin_required
def admin_movies():
    db = get_db()
    movies_list = query(db, """
        SELECT m.*,
               COALESCE((SELECT COUNT(*) FROM showings sh
                         JOIN seats st ON st.showing_id = sh.id
                        WHERE sh.movie_id = m.id
                          AND st.status = 'available'), 0) AS avail_seats
        FROM movies m
        ORDER BY m.created_at DESC
    """)
    db.close()
    return render_template('admin_movies.html', movies=movies_list)

@app.route('/admin/movies/add', methods=['POST'])
@admin_required
def admin_movies_add():
    title        = request.form.get('title', '').strip()
    genre        = request.form.get('genre', '').strip()
    cast_members = request.form.get('cast_members', '').strip()
    duration     = request.form.get('duration_mins', '120').strip()
    rating       = request.form.get('rating', '0').strip() or '0'
    release_date = request.form.get('release_date', '').strip() or None
    status_val   = request.form.get('status', 'active')
    description  = request.form.get('description', '').strip()

    if not title or not genre or not duration:
        flash('Title, genre, and duration are required.', 'error')
        return redirect(url_for('admin_movies'))

    poster_path = 'images/no_poster.png'
    poster_file = request.files.get('poster')
    if poster_file and poster_file.filename and allowed_file(poster_file.filename):
        from werkzeug.utils import secure_filename
        filename = secure_filename(poster_file.filename)
        save_dir = os.path.join(os.path.dirname(__file__), 'static', 'images', 'movies')
        os.makedirs(save_dir, exist_ok=True)
        poster_file.save(os.path.join(save_dir, filename))
        poster_path = f'images/movies/{filename}'

    try:
        db = get_db()
        execute(db, """
            INSERT INTO movies
                (title, genre, cast_members, duration_mins, rating,
                 release_date, status, description, poster_path)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (title, genre, cast_members, int(duration), float(rating),
              release_date, status_val, description, poster_path))
        db.commit(); db.close()
        flash(f'Movie "{title}" added!', 'success')
    except Exception as e:
        flash(f'Error adding movie: {e}', 'error')
    return redirect(url_for('admin_movies'))

@app.route('/admin/movies/edit/<int:movie_id>', methods=['POST'])
@admin_required
def admin_movies_edit(movie_id):
    title        = request.form.get('title', '').strip()
    genre        = request.form.get('genre', '').strip()
    cast_members = request.form.get('cast_members', '').strip()
    duration     = request.form.get('duration_mins', '120').strip()
    rating       = request.form.get('rating', '0').strip() or '0'
    release_date = request.form.get('release_date', '').strip() or None
    status_val   = request.form.get('status', 'active')
    description  = request.form.get('description', '').strip()

    if not title or not genre or not duration:
        flash('Title, genre, and duration are required.', 'error')
        return redirect(url_for('admin_movies'))
    try:
        db = get_db()
        poster_file = request.files.get('poster')
        if poster_file and poster_file.filename and allowed_file(poster_file.filename):
            from werkzeug.utils import secure_filename
            filename = secure_filename(poster_file.filename)
            save_dir = os.path.join(os.path.dirname(__file__), 'static', 'images', 'movies')
            os.makedirs(save_dir, exist_ok=True)
            poster_file.save(os.path.join(save_dir, filename))
            execute(db, """
                UPDATE movies SET title=%s,genre=%s,cast_members=%s,duration_mins=%s,
                                  rating=%s,release_date=%s,status=%s,description=%s,
                                  poster_path=%s WHERE id=%s
            """, (title,genre,cast_members,int(duration),float(rating),
                  release_date,status_val,description,f'images/movies/{filename}',movie_id))
        else:
            execute(db, """
                UPDATE movies SET title=%s,genre=%s,cast_members=%s,duration_mins=%s,
                                  rating=%s,release_date=%s,status=%s,description=%s WHERE id=%s
            """, (title,genre,cast_members,int(duration),float(rating),
                  release_date,status_val,description,movie_id))
        db.commit(); db.close()
        flash(f'Movie "{title}" updated!', 'success')
    except Exception as e:
        flash(f'Error updating movie: {e}', 'error')
    return redirect(url_for('admin_movies'))

@app.route('/admin/movies/delete', methods=['POST'])
@admin_required
def admin_movies_delete():
    movie_id = request.form.get('movie_id', type=int)
    if not movie_id:
        flash('Invalid movie.', 'error')
        return redirect(url_for('admin_movies'))
    try:
        db = get_db()
        movie = query(db, "SELECT title FROM movies WHERE id=%s", (movie_id,), one=True)
        if movie:
            execute(db, """
                DELETE seats FROM seats
                  JOIN showings ON showings.id = seats.showing_id
                WHERE showings.movie_id=%s
            """, (movie_id,))
            execute(db, """
                DELETE bookings FROM bookings
                  JOIN showings ON showings.id = bookings.showing_id
                WHERE showings.movie_id=%s
            """, (movie_id,))
            execute(db, "DELETE FROM showings WHERE movie_id=%s", (movie_id,))
            execute(db, "DELETE FROM movies WHERE id=%s", (movie_id,))
            db.commit()
            flash(f'Movie "{movie["title"]}" deleted.', 'success')
        db.close()
    except Exception as e:
        flash(f'Error deleting movie: {e}', 'error')
    return redirect(url_for('admin_movies'))

# ─────────────────────────────────────────────────────────────
#  ADMIN — BOOKINGS
# ─────────────────────────────────────────────────────────────
@app.route('/admin/bookings')
@admin_required
def admin_bookings():
    db = get_db()
    bookings_list = query(db, """
        SELECT b.id, b.booking_ref, b.customer_name, b.contact, b.total_price,
               b.status, b.ticket_count, b.ticket_type, b.seat_codes,
               b.discount_status, b.payment_status,
               m.title AS movie_title, c.name AS cinema_name,
               s.show_date, s.show_time
        FROM bookings b
        JOIN showings s ON b.showing_id=s.id
        JOIN movies   m ON s.movie_id=m.id
        JOIN cinemas  c ON s.cinema_id=c.id
        ORDER BY b.id DESC
    """)
    db.close()
    return render_template('admin_bookings.html', bookings=bookings_list)

@app.route('/admin/bookings/cancel', methods=['POST'])
@admin_required
def admin_bookings_cancel():
    ref_code = request.form.get('ref_code', '').strip()
    if not ref_code:
        flash('Invalid booking reference.', 'error')
        return redirect(url_for('admin_bookings'))
    try:
        db = get_db()
        execute(db, "UPDATE bookings SET status='Cancelled' WHERE ref_code=%s", (ref_code,))
        seat_rows = query(db, "SELECT seat_id FROM bookings WHERE ref_code=%s", (ref_code,))
        for s in seat_rows:
            execute(db,
                "UPDATE seats SET status='available', locked_until=NULL WHERE id=%s", (s['seat_id'],))
        execute(db, "UPDATE payments SET status='refunded' WHERE booking_ref=%s", (ref_code,))
        db.commit(); db.close()
        flash(f'Booking {ref_code} cancelled.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'error')
    return redirect(url_for('admin_bookings'))

# ─────────────────────────────────────────────────────────────
#  ADMIN — DISCOUNT VERIFICATIONS
# ─────────────────────────────────────────────────────────────
@app.route('/admin/verifications')
@admin_required
def admin_verifications():
    db = get_db()
    pending = query(db, """
        SELECT b.id, b.ref_code, b.customer_name, b.contact,
               b.ticket_type, b.discount_status, b.created_at,
               m.title AS movie, c.name AS cinema, s.show_date, s.show_time
        FROM bookings b
        JOIN showings s ON s.id=b.showing_id
        JOIN movies   m ON m.id=s.movie_id
        JOIN cinemas  c ON c.id=s.cinema_id
        WHERE b.discount_status IN ('pending_verification','verified','rejected')
        GROUP BY b.ref_code
        ORDER BY b.created_at DESC
    """)
    db.close()
    return render_template('admin_verification.html', verifications=pending)

@app.route('/admin/verifications/approve', methods=['POST'])
@admin_required
def admin_verify_approve():
    ref_code = request.form.get('ref_code', '').strip()
    if ref_code:
        db = get_db()
        execute(db, "UPDATE bookings SET discount_status='verified' WHERE ref_code=%s", (ref_code,))
        db.commit(); db.close()
        flash(f'Discount for {ref_code} approved.', 'success')
    return redirect(url_for('admin_verifications'))

@app.route('/admin/verifications/reject', methods=['POST'])
@admin_required
def admin_verify_reject():
    ref_code = request.form.get('ref_code', '').strip()
    if ref_code:
        db = get_db()
        execute(db, """
            UPDATE bookings SET discount_status='rejected', ticket_type='Regular',
                                unit_price=%s, total_price=(ticket_count * %s)
             WHERE ref_code=%s
        """, (450, 450, ref_code))
        db.commit(); db.close()
        flash(f'Discount for {ref_code} rejected.', 'warning')
    return redirect(url_for('admin_verifications'))

# ─────────────────────────────────────────────────────────────
#  ADMIN — PAYMENTS
# ─────────────────────────────────────────────────────────────
@app.route('/admin/payments')
@admin_required
def admin_payments():
    db = get_db()
    payments = query(db, """
        SELECT p.*, b.customer_name, b.ticket_type, m.title AS movie
        FROM payments p
        LEFT JOIN bookings b ON b.ref_code=p.booking_ref
        LEFT JOIN showings s ON s.id=b.showing_id
        LEFT JOIN movies   m ON m.id=s.movie_id
        GROUP BY p.id
        ORDER BY p.created_at DESC
    """)
    db.close()
    return render_template('admin_payments.html', payments=payments)

# ─────────────────────────────────────────────────────────────
#  ADMIN — USERS
# ─────────────────────────────────────────────────────────────
@app.route('/admin/users')
@admin_required
def admin_users():
    db = get_db()
    users_list = query(db, """
        SELECT u.*,
               COALESCE((SELECT COUNT(*) FROM bookings b WHERE b.user_id=u.id),0) AS booking_count
        FROM users u ORDER BY u.id DESC
    """)
    db.close()
    return render_template('admin_users.html', users=users_list)

@app.route('/admin/users/delete', methods=['POST'])
@admin_required
def admin_users_delete():
    user_id = request.form.get('user_id', type=int)
    if not user_id:
        flash('Invalid user.', 'error')
        return redirect(url_for('admin_users'))
    try:
        db = get_db()
        user = query(db, "SELECT full_name FROM users WHERE id=%s", (user_id,), one=True)
        if user:
            execute(db, "DELETE FROM bookings WHERE user_id=%s", (user_id,))
            execute(db, "DELETE FROM users WHERE id=%s", (user_id,))
            db.commit()
            flash(f'User "{user["full_name"]}" deleted.', 'success')
        db.close()
    except Exception as e:
        flash(f'Error: {e}', 'error')
    return redirect(url_for('admin_users'))

# ─────────────────────────────────────────────────────────────
#  STARTUP
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
#  STUB ROUTES — placeholder pages (not yet implemented)
# ─────────────────────────────────────────────────────────────
@app.route('/profile')
@login_required
def profile():
    return render_template('my_bookings.html',
                           user_name=session.get('user_name') or session.get('admin_name', 'Admin'),
                           bookings=[])

@app.route('/settings')
@login_required
def settings():
    flash('Settings page coming soon.', 'info')
    return redirect(url_for('index'))

@app.route('/change-password')
@login_required
def change_password():
    flash('Change password page coming soon.', 'info')
    return redirect(url_for('index'))

@app.route('/notifications')
@login_required
def notifications():
    flash('Notifications coming soon.', 'info')
    return redirect(url_for('index'))

@app.route('/help')
@login_required
def help_page():
    flash('Help & Support coming soon.', 'info')
    return redirect(url_for('index'))

@app.route('/forgot-password')
def forgot_password():
    flash('Password reset coming soon. Contact support.', 'info')
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)