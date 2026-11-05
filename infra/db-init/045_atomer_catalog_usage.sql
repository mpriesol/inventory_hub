ALTER TABLE atomer_catalog_campaigns ADD COLUMN IF NOT EXISTS next_category_at timestamptz NOT NULL DEFAULT now();
CREATE TABLE IF NOT EXISTS atomer_catalog_request_usage (
 campaign text NOT NULL REFERENCES atomer_catalog_campaigns(id), bucket timestamptz NOT NULL,
 phase text NOT NULL, shop text NOT NULL, resource text NOT NULL, method text NOT NULL,
 event text NOT NULL CHECK(event IN ('sent','deferred','http429')), amount integer NOT NULL,
 PRIMARY KEY(campaign,bucket,phase,shop,resource,method,event)
);
