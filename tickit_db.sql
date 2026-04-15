-- ============================================================
--  TICK.IT — MySQL Schema
--  Run this in MySQL Workbench before starting the Flask app
--
--  For a FRESH install: run the entire file.
--  For an EXISTING DB:  see the "MIGRATIONS" section at bottom.
-- ============================================================

CREATE DATABASE IF NOT EXISTS tickit_db
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE tickit_db;

-- ── USERS ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    email      VARCHAR(255) UNIQUE,
    mobile     VARCHAR(20)  UNIQUE,
    full_name  VARCHAR(255) NOT NULL,
    age        TINYINT UNSIGNED NOT NULL DEFAULT 0,
    gender     ENUM('Male','Female','Non-binary','Prefer not to say') NOT NULL DEFAULT 'Prefer not to say',
    address    VARCHAR(500) NOT NULL DEFAULT '',
    password   VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_contact CHECK (email IS NOT NULL OR mobile IS NOT NULL)
);

-- ── MOVIES ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS movies (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    title         VARCHAR(255) NOT NULL,
    genre         VARCHAR(100) NOT NULL,
    rating        DECIMAL(3,1) NOT NULL DEFAULT 0.0,
    poster_path   VARCHAR(500) NOT NULL DEFAULT 'images/no_poster.png',
    duration_mins SMALLINT UNSIGNED NOT NULL DEFAULT 120,
    description   TEXT,
    cast_members  VARCHAR(500),
    release_date  DATE,
    status        ENUM('active','inactive') NOT NULL DEFAULT 'active',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── CINEMAS ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cinemas (
    id       INT AUTO_INCREMENT PRIMARY KEY,
    name     VARCHAR(255) NOT NULL,
    location VARCHAR(500) NOT NULL,
    screens  TINYINT UNSIGNED NOT NULL DEFAULT 1
);

-- ── CINEMA HALLS ──────────────────────────────────────────────
-- Create before showings so the FK reference is valid
CREATE TABLE IF NOT EXISTS cinema_halls (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    cinema_id   INT NOT NULL,
    hall_name   VARCHAR(100) NOT NULL,
    rows_count  TINYINT UNSIGNED NOT NULL DEFAULT 8,
    cols_count  TINYINT UNSIGNED NOT NULL DEFAULT 10,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cinema_id) REFERENCES cinemas(id) ON DELETE CASCADE,
    UNIQUE KEY uq_hall (cinema_id, hall_name)
);

-- ── SHOWINGS ─────────────────────────────────────────────────
-- hall_id is nullable: each showing is assigned to a specific hall.
-- UNIQUE on (hall_id, show_date, show_time) so one hall can only have
-- one movie at a given date+time slot.
-- (movie_id, cinema_id, show_date, show_time) unique key is removed
--  because two different halls in same cinema can run same movie.
CREATE TABLE IF NOT EXISTS showings (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    movie_id    INT NOT NULL,
    cinema_id   INT NOT NULL,
    hall_id     INT NULL,
    show_date   DATE NOT NULL,
    show_time   TIME NOT NULL,
    total_seats TINYINT UNSIGNED NOT NULL DEFAULT 50,
    status      ENUM('scheduled','open','full','completed') NOT NULL DEFAULT 'open',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (movie_id)  REFERENCES movies(id)  ON DELETE CASCADE,
    FOREIGN KEY (cinema_id) REFERENCES cinemas(id) ON DELETE CASCADE,
    FOREIGN KEY (hall_id)   REFERENCES cinema_halls(id) ON DELETE SET NULL,
    UNIQUE KEY uq_showing_hall (hall_id, show_date, show_time)
);

-- ── SEATS ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS seats (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    showing_id   INT NOT NULL,
    row_label    CHAR(1) NOT NULL,
    seat_number  TINYINT UNSIGNED NOT NULL,
    seat_code    VARCHAR(6) NOT NULL,
    category     ENUM('VIP','Standard') NOT NULL DEFAULT 'Standard',
    status       ENUM('available','locked','booked') NOT NULL DEFAULT 'available',
    locked_until DATETIME NULL,
    FOREIGN KEY (showing_id) REFERENCES showings(id) ON DELETE CASCADE,
    UNIQUE KEY uq_seat (showing_id, seat_code)
);

-- ── BOOKINGS ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bookings (
    id                   INT AUTO_INCREMENT PRIMARY KEY,
    user_id              INT NOT NULL,
    showing_id           INT NOT NULL,
    seat_id              INT NOT NULL,
    booking_ref          VARCHAR(20),
    ref_code             VARCHAR(20) NOT NULL,
    ticket_type          ENUM('Regular','Student','Senior / PWD') NOT NULL DEFAULT 'Regular',
    ticket_count         TINYINT UNSIGNED NOT NULL DEFAULT 1,
    unit_price           SMALLINT UNSIGNED NOT NULL DEFAULT 450,
    total_price          DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    seat_codes           VARCHAR(500),
    customer_name        VARCHAR(255) NOT NULL,
    contact              VARCHAR(20)  NOT NULL,
    special_requests     TEXT,
    discount_status      ENUM('none','pending_verification','verified','rejected') NOT NULL DEFAULT 'none',
    payment_status       ENUM('pending','paid','failed','refunded') NOT NULL DEFAULT 'pending',
    verification_details TEXT NULL,
    status               ENUM('Confirmed','Cancelled','Completed') NOT NULL DEFAULT 'Confirmed',
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id)    REFERENCES users(id)    ON DELETE CASCADE,
    FOREIGN KEY (showing_id) REFERENCES showings(id) ON DELETE CASCADE,
    FOREIGN KEY (seat_id)    REFERENCES seats(id)    ON DELETE CASCADE,
    INDEX idx_ref_code (ref_code),
    INDEX idx_user_id  (user_id)
);

-- ── HALL SEAT CONFIG ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hall_seat_config (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    hall_id     INT NOT NULL,
    row_label   CHAR(1) NOT NULL,
    col_number  TINYINT UNSIGNED NOT NULL,
    seat_code   VARCHAR(6) NOT NULL,
    seat_type   ENUM('Regular','VIP','PWD') NOT NULL DEFAULT 'Regular',
    is_active   TINYINT(1) NOT NULL DEFAULT 1,
    FOREIGN KEY (hall_id) REFERENCES cinema_halls(id) ON DELETE CASCADE,
    UNIQUE KEY uq_hall_seat (hall_id, seat_code)
);

-- ── PAYMENTS ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS payments (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    booking_ref       VARCHAR(20) NOT NULL,
    user_id           INT NULL,
    amount            DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    payment_method    VARCHAR(50) NOT NULL DEFAULT 'walk_in',
    paymongo_link_id  VARCHAR(100) NULL,
    status            ENUM('pending','paid','failed','refunded') NOT NULL DEFAULT 'pending',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    paid_at           DATETIME NULL,
    failed_at         DATETIME NULL,
    INDEX idx_booking_ref (booking_ref),
    INDEX idx_user_id     (user_id)
);

-- ── SEED CINEMAS ─────────────────────────────────────────────
INSERT IGNORE INTO cinemas (name, location, screens) VALUES
    ('SM Seaside Cebu',           'SRP, Cebu City',        6),
    ('Gaisano Grand Minglanilla', 'Minglanilla, Cebu',     4),
    ('Nustar Cebu Cinema',        'SRP, Cebu City',        5),
    ('Cebu IL CORSO Cinema',      'South Road Properties', 4),
    ('UC Cantao-an',              'Naga, Cebu',            2),
    ('TOPS Cebu Skydom',          'Busay, Cebu City',      3);


-- ============================================================
--  MIGRATIONS — run these ONLY on an EXISTING database
--  (skip if you are running a fresh install above)
-- ============================================================

-- Add hall_id to showings (if upgrading from old schema without it)
-- ALTER TABLE showings ADD COLUMN IF NOT EXISTS hall_id INT NULL AFTER cinema_id;
-- ALTER TABLE showings ADD CONSTRAINT fk_showing_hall FOREIGN KEY (hall_id) REFERENCES cinema_halls(id) ON DELETE SET NULL;

-- Update showings unique key to be hall-based (run in order):
-- ALTER TABLE showings DROP INDEX uq_showing;
-- ALTER TABLE showings ADD UNIQUE KEY uq_showing_hall (hall_id, show_date, show_time);

-- Add discount_status, payment_status, verification_details to bookings (if missing):
-- ALTER TABLE bookings
--     ADD COLUMN IF NOT EXISTS discount_status ENUM('none','pending_verification','verified','rejected') NOT NULL DEFAULT 'none' AFTER special_requests,
--     ADD COLUMN IF NOT EXISTS payment_status  ENUM('pending','paid','failed','refunded') NOT NULL DEFAULT 'pending' AFTER discount_status,
--     ADD COLUMN IF NOT EXISTS verification_details TEXT NULL AFTER payment_status;
