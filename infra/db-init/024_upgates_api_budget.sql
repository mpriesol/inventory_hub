-- Shared transport admission only; no stock, order or automation activation.
CREATE TABLE IF NOT EXISTS upgates_api_budget (
    target_key char(64) PRIMARY KEY,
    hour_remaining bigint CHECK (hour_remaining BETWEEN 0 AND 1000000000),
    day_remaining bigint CHECK (day_remaining BETWEEN 0 AND 1000000000),
    observed_hour timestamptz,
    blocked_until timestamptz,
    requests_started bigint NOT NULL DEFAULT 0 CHECK (requests_started >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
