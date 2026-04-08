-- ============================================
-- TRACKER DATABASE SCHEMA (SIMPLIFIED)
-- TimescaleDB + PostGIS
-- ============================================

-- Rozšírenia
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================
-- 1. DEVICES (Trackery)
-- ============================================
CREATE TABLE devices (
    device_id TEXT PRIMARY KEY,           -- DevEUI
    device_name TEXT,                     -- User-friendly name
    device_type TEXT DEFAULT 'T1000-B',
    color TEXT DEFAULT '#FF0000',         -- Farba na mape
    active BOOLEAN DEFAULT TRUE,
    last_seen TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_devices_active ON devices(active);
CREATE INDEX idx_devices_last_seen ON devices(last_seen DESC);

COMMENT ON TABLE devices IS 'Metadata o GPS trackeroch';

-- ============================================
-- 2. POSITIONS (GPS záznamy - TIME-SERIES!)
-- ============================================
CREATE TABLE positions (
    id BIGSERIAL,
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- GPS súradnice (JEDINÉ NOT NULL!)
    latitude DOUBLE PRECISION NOT NULL CHECK (latitude >= -90 AND latitude <= 90),
    longitude DOUBLE PRECISION NOT NULL CHECK (longitude >= -180 AND longitude <= 180),
    location GEOGRAPHY(POINT, 4326),  -- PostGIS automaticky

    -- Všetko ostatné NULLABLE (batéria nechodí v každej správe!)
    altitude INTEGER,
    battery SMALLINT CHECK (battery >= 0 AND battery <= 100),
    signal_strength INTEGER,  -- RSSI
    signal_quality REAL,      -- SNR
    speed REAL,
    heading SMALLINT CHECK (heading >= 0 AND heading <= 360),
    accuracy REAL,

    -- LoRaWAN metadata
    gateway_id TEXT,
    frame_counter INTEGER,
    raw_payload TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW(),

    -- Composite primary key pre TimescaleDB
    PRIMARY KEY (timestamp, id)
);

-- Konverzia na TimescaleDB hypertable (time-series optimalizácia)
SELECT create_hypertable(
    'positions',
    'timestamp',
    chunk_time_interval => INTERVAL '1 week',
    if_not_exists => TRUE
);

-- Indexy pre rýchle queries
CREATE INDEX idx_positions_device ON positions(device_id, timestamp DESC);
CREATE INDEX idx_positions_location ON positions USING GIST(location);

-- Kompresná politika (staršie dáta sa automaticky komprimujú)
ALTER TABLE positions SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id'
);

SELECT add_compression_policy('positions', INTERVAL '7 days');

COMMENT ON TABLE positions IS 'GPS pozície - TimescaleDB hypertable';

-- ============================================
-- 3. MATERIALIZED VIEW (Real-time dashboard)
-- ============================================
CREATE MATERIALIZED VIEW latest_positions AS
SELECT DISTINCT ON (device_id)
    device_id,
    timestamp,
    latitude,
    longitude,
    battery,
    signal_strength,
    signal_quality,
    location,
    EXTRACT(EPOCH FROM (NOW() - timestamp)) / 60 AS minutes_ago
FROM positions
ORDER BY device_id, timestamp DESC;

CREATE UNIQUE INDEX idx_latest_positions_device ON latest_positions(device_id);

COMMENT ON MATERIALIZED VIEW latest_positions IS 'Refresh každých 30s pre real-time view';

-- ============================================
-- 4. TRIGGERY
-- ============================================

-- Auto-populate PostGIS location
CREATE OR REPLACE FUNCTION populate_location()
RETURNS TRIGGER AS $$
BEGIN
    NEW.location = ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326)::geography;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_location BEFORE INSERT OR UPDATE ON positions
    FOR EACH ROW EXECUTE FUNCTION populate_location();

-- Auto-update last_seen v devices
CREATE OR REPLACE FUNCTION update_last_seen()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE devices
    SET last_seen = NEW.timestamp,
        updated_at = NOW()
    WHERE device_id = NEW.device_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_device_last_seen AFTER INSERT ON positions
    FOR EACH ROW EXECUTE FUNCTION update_last_seen();

-- ============================================
-- 5. UŽITOČNÉ FUNKCIE
-- ============================================

-- Získaj trasu zariadenia v časovom rozmedzí
CREATE OR REPLACE FUNCTION get_device_trail(
    target_device_id TEXT,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ
)
RETURNS TABLE (
    timestamp TIMESTAMPTZ,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    battery SMALLINT,
    geojson JSON
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        p.timestamp,
        p.latitude,
        p.longitude,
        p.battery,
        ST_AsGeoJSON(p.location)::json AS geojson
    FROM positions p
    WHERE p.device_id = target_device_id
      AND p.timestamp BETWEEN start_time AND end_time
    ORDER BY p.timestamp ASC;
END;
$$ LANGUAGE plpgsql;

-- Nájdi najbližšie zariadenia
CREATE OR REPLACE FUNCTION get_nearby_devices(
    target_lat DOUBLE PRECISION,
    target_lon DOUBLE PRECISION,
    max_distance_km REAL DEFAULT 5.0
)
RETURNS TABLE (
    device_id TEXT,
    device_name TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    distance_km REAL
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        lp.device_id,
        d.device_name,
        lp.latitude,
        lp.longitude,
        (ST_Distance(
            lp.location,
            ST_SetSRID(ST_MakePoint(target_lon, target_lat), 4326)::geography
        ) / 1000.0)::REAL AS distance_km
    FROM latest_positions lp
    JOIN devices d ON lp.device_id = d.device_id
    WHERE ST_DWithin(
        lp.location,
        ST_SetSRID(ST_MakePoint(target_lon, target_lat), 4326)::geography,
        max_distance_km * 1000
    )
    ORDER BY distance_km;
END;
$$ LANGUAGE plpgsql;

-- ============================================
-- 6. DEMO DATA (pre testovanie)
-- ============================================

-- Vytvor testovací tracker (ak ešte neexistuje)
INSERT INTO devices (device_id, device_name, device_type, color)
VALUES ('2cf7f1c05300063d', 'TestingTracker2', 'T1000-B', '#FF0000')
ON CONFLICT (device_id) DO NOTHING;

-- ============================================
-- HOTOVO!
-- ============================================

-- Výpis všetkých tabuliek a veľkostí
SELECT
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;

-- Info o TimescaleDB hypertable
SELECT * FROM timescaledb_information.hypertables;