-- 0006_platform_settings_defaults -- the operator switches' default values.
--
-- The squashed baseline is schema-only, so the row the internal migration
-- `platform_verification_switch` wrote never reached a published install.
-- A missing row reads as off, so behaviour was the same -- but the default
-- is meant to be a FACT IN THE DATABASE, where an operator sees it and the
-- ledger records who changed it, not an absence the code interprets.
--
-- A NEW migration rather than an edit to 0002: an install already past
-- 0002 must get this row too. ON CONFLICT DO NOTHING, so an operator's
-- choice is never overwritten.

INSERT INTO platform_settings (key, value, updated_by)
VALUES ('verification_enabled', false, 'migration: off by default')
ON CONFLICT (key) DO NOTHING;
