-- Kairos views (plan §6). SAP HANA Cloud dialect.
-- db/views_sqlite.sql is GENERATED from this file:  python -m ingest.load_hana --gen-sqlite
--
-- Portability rules (the generator rewrites only these HANA functions):
--   ADD_DAYS(d, -n)      -> DATE(d, '-n days')
--   DAYS_BETWEEN(a, b)   -> CAST(JULIANDAY(b) - JULIANDAY(a) AS INTEGER)
--   LEAST / GREATEST     -> MIN / MAX
-- Arguments of ADD_DAYS / DAYS_BETWEEN must be plain columns or integer literals.
-- No CTEs; derived tables and window functions only (both engines support them).
-- Views are listed in dependency order.

-- "Today" for every view: FF_CFG_PARAM.AS_OF_DATE if set, else CURRENT_DATE. Always one row.
CREATE VIEW FF_V_ASOF AS
SELECT COALESCE(MAX(CASE WHEN NAME = 'AS_OF_DATE' THEN DATE_VALUE END), CURRENT_DATE) AS AS_OF
FROM FF_CFG_PARAM;

-- Net goods issue to wards over the 90 days ending AS_OF (261/201 minus reversals 262/202).
CREATE VIEW FF_V_DAILY_ISSUE_90D AS
SELECT m.MATNR,
       m.WERKS,
       SUM(CASE WHEN m.BWART IN ('261', '201') THEN m.MENGE ELSE -m.MENGE END) AS ISSUE_QTY_90D,
       SUM(CASE WHEN m.BWART IN ('261', '201') THEN m.MENGE ELSE -m.MENGE END) / 90.0 AS AVG_DAILY_ISSUE
FROM FF_MM_MSEG m
JOIN FF_V_ASOF a ON 1 = 1
WHERE m.BWART IN ('261', '201', '262', '202')
  AND m.BUDAT > ADD_DAYS(a.AS_OF, -90)
  AND m.BUDAT <= a.AS_OF
GROUP BY m.MATNR, m.WERKS;

-- Per-batch FEFO usable quantity. Only unrestricted (CLABS), non-expired batches count.
-- Batches are consumed earliest-expiry first at the 90-day average rate r. A batch expiring in
-- d days can only be used up to r*d minus everything queued ahead of it, so stock that would
-- expire on the shelf is not counted as cover. Queued-ahead uses full quantities, which makes
-- this a conservative (lower-bound) estimate. With no issue history, all unexpired stock counts.
CREATE VIEW FF_V_BATCH_FEFO AS
SELECT q.MATNR,
       q.WERKS,
       q.LGORT,
       q.CHARG,
       q.VFDAT,
       q.CLABS,
       q.DAYS_TO_EXPIRY,
       q.QTY_AHEAD,
       CASE WHEN COALESCE(q.AVG_DAILY_ISSUE, 0) <= 0 THEN q.CLABS
            ELSE LEAST(q.CLABS, GREATEST(0, q.AVG_DAILY_ISSUE * q.DAYS_TO_EXPIRY - q.QTY_AHEAD))
       END AS USABLE_QTY
FROM (
    SELECT b.MATNR,
           b.WERKS,
           b.LGORT,
           b.CHARG,
           b.VFDAT,
           b.CLABS,
           DAYS_BETWEEN(a.AS_OF, b.VFDAT) AS DAYS_TO_EXPIRY,
           COALESCE(SUM(b.CLABS) OVER (PARTITION BY b.MATNR, b.WERKS
                                       ORDER BY b.VFDAT, b.LGORT, b.CHARG
                                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS QTY_AHEAD,
           i.AVG_DAILY_ISSUE
    FROM FF_MM_MCHB b
    JOIN FF_V_ASOF a ON 1 = 1
    LEFT JOIN FF_V_DAILY_ISSUE_90D i ON i.MATNR = b.MATNR AND i.WERKS = b.WERKS
    WHERE b.CLABS > 0
      AND b.VFDAT > a.AS_OF
) q;

-- Days of cover per material and plant = FEFO usable stock / average daily issue (90 days).
-- Materials that are issued but have no usable stock appear with DAYS_OF_COVER = 0.
-- DAYS_OF_COVER is NULL when there was no net issue in the window.
CREATE VIEW FF_V_DAYS_OF_COVER AS
SELECT k.MATNR,
       k.WERKS,
       mara.FORM_ID,
       COALESCE(s.UNEXPIRED_QTY, 0) AS UNEXPIRED_QTY,
       COALESCE(s.USABLE_QTY, 0) AS USABLE_QTY,
       COALESCE(i.AVG_DAILY_ISSUE, 0) AS AVG_DAILY_ISSUE_90D,
       CASE WHEN i.AVG_DAILY_ISSUE > 0
            THEN ROUND(COALESCE(s.USABLE_QTY, 0) / i.AVG_DAILY_ISSUE, 1)
       END AS DAYS_OF_COVER,
       s.NEXT_EXPIRY
FROM (
    SELECT MATNR, WERKS FROM FF_MM_MCHB
    UNION
    SELECT MATNR, WERKS FROM FF_V_DAILY_ISSUE_90D
) k
LEFT JOIN (
    SELECT MATNR, WERKS,
           SUM(CLABS) AS UNEXPIRED_QTY,
           SUM(USABLE_QTY) AS USABLE_QTY,
           MIN(VFDAT) AS NEXT_EXPIRY
    FROM FF_V_BATCH_FEFO
    GROUP BY MATNR, WERKS
) s ON s.MATNR = k.MATNR AND s.WERKS = k.WERKS
LEFT JOIN FF_V_DAILY_ISSUE_90D i ON i.MATNR = k.MATNR AND i.WERKS = k.WERKS
LEFT JOIN FF_MM_MARA mara ON mara.MATNR = k.MATNR;

-- On-time delivery per supplier. Actual delivery = first 101 goods receipt against the PO line.
-- A line is on time when that GR is on or before EINDT. Open lines have no GR yet.
CREATE VIEW FF_V_SUPPLIER_OTD AS
SELECT h.LIFNR,
       l.NAME1,
       l.SPERR,
       COUNT(*) AS PO_LINES,
       SUM(CASE WHEN g.GR_DATE IS NOT NULL THEN 1 ELSE 0 END) AS DELIVERED_LINES,
       SUM(CASE WHEN g.GR_DATE <= p.EINDT THEN 1 ELSE 0 END) AS ON_TIME_LINES,
       CASE WHEN SUM(CASE WHEN g.GR_DATE IS NOT NULL THEN 1 ELSE 0 END) > 0
            THEN ROUND(100.0 * SUM(CASE WHEN g.GR_DATE <= p.EINDT THEN 1 ELSE 0 END)
                       / SUM(CASE WHEN g.GR_DATE IS NOT NULL THEN 1 ELSE 0 END), 1)
       END AS OTD_PCT,
       AVG(CASE WHEN g.GR_DATE > p.EINDT THEN DAYS_BETWEEN(p.EINDT, g.GR_DATE) END) AS AVG_DELAY_DAYS_LATE,
       MAX(g.GR_DATE) AS LAST_GR_DATE
FROM FF_MM_EKPO p
JOIN FF_MM_EKKO h ON h.EBELN = p.EBELN
LEFT JOIN (
    SELECT EBELN, EBELP, MIN(BUDAT) AS GR_DATE
    FROM FF_MM_MSEG
    WHERE BWART = '101' AND EBELN IS NOT NULL
    GROUP BY EBELN, EBELP
) g ON g.EBELN = p.EBELN AND g.EBELP = p.EBELP
LEFT JOIN FF_MM_LFA1 l ON l.LIFNR = h.LIFNR
GROUP BY h.LIFNR, l.NAME1, l.SPERR;

-- NSQ alert batch -> matching hospital batches in every plant/storage location.
-- Matching is by batch number; manufacturer and formulation must agree when both sides have them.
-- MATCH_LEVEL tells the reviewer how strong the match is (it is a lead, not a confirmed recall).
CREATE VIEW FF_V_RECALL_TRACE AS
SELECT n.ALERT_ID,
       n.MONTH AS ALERT_MONTH,
       n.DRUG,
       n.BATCH,
       n.MANUFACTURER_ID,
       n.REASON,
       b.MATNR,
       mara.FORM_ID,
       b.WERKS,
       w.NAME1 AS PLANT_NAME,
       b.LGORT,
       b.CHARG,
       b.CLABS,
       b.CINSM,
       b.CSPEM,
       b.VFDAT,
       CASE WHEN n.MANUFACTURER_ID IS NOT NULL AND b.MANUFACTURER_ID IS NOT NULL
                 AND n.FORM_ID IS NOT NULL AND mara.FORM_ID IS NOT NULL THEN 'BATCH+MFR+FORM'
            WHEN n.MANUFACTURER_ID IS NOT NULL AND b.MANUFACTURER_ID IS NOT NULL THEN 'BATCH+MFR'
            WHEN n.FORM_ID IS NOT NULL AND mara.FORM_ID IS NOT NULL THEN 'BATCH+FORM'
            ELSE 'BATCH_ONLY'
       END AS MATCH_LEVEL
FROM FF_REF_NSQ_ALERT n
JOIN FF_MM_MCHB b ON UPPER(TRIM(b.CHARG)) = UPPER(TRIM(n.BATCH))
LEFT JOIN FF_MM_MARA mara ON mara.MATNR = b.MATNR
LEFT JOIN FF_MM_T001W w ON w.WERKS = b.WERKS
WHERE (n.MANUFACTURER_ID IS NULL OR b.MANUFACTURER_ID IS NULL OR n.MANUFACTURER_ID = b.MANUFACTURER_ID)
  AND (n.FORM_ID IS NULL OR mara.FORM_ID IS NULL OR n.FORM_ID = mara.FORM_ID);

-- One row per formulation: current ceiling price (as of AS_OF, excl. GST), producer lower bound
-- ("at least N"), tightest days of cover across plants, and the latest A1/A2/A3 output (if any).
-- "Latest" = the most recent run (FF_AG_RUN.STARTED_AT) that has an A3 forecast for the formulation.
CREATE VIEW FF_V_WATCHLIST AS
SELECT f.FORM_ID,
       f.GENERIC,
       f.DOSAGE_FORM,
       f.STRENGTH,
       f.NLEM_LEVEL,
       f.THERAPEUTIC_CLASS,
       cp.CEILING_PRICE,
       cp.GST_RATE,
       cp.EFFECTIVE_FROM AS CEILING_EFFECTIVE_FROM,
       cp.SO_NUMBER AS CEILING_SO_NUMBER,
       cp.PARA AS CEILING_PARA,
       COALESCE(pc.PRODUCER_COUNT_MIN, 0) AS PRODUCER_COUNT_MIN,
       dc.MIN_DAYS_OF_COVER,
       fc.RUN_ID,
       fc.STARTED_AT AS ASSESSED_AT,
       fc.EXIT_RISK_BAND,
       fc.EXIT_RISK,
       fc.CAUSE_CODE,
       fc.CONFIDENCE,
       sg.BAND AS MARGIN_BAND,
       sg.HEADROOM_PCT,
       sg.MONTHS_TO_BREACH,
       dp.HHI,
       dp.TOP_ORIGIN_COUNTRY,
       dp.TOP_ORIGIN_SHARE,
       f.IS_PROXY
FROM FF_REF_FORMULATION f
LEFT JOIN (
    SELECT x.FORM_ID, x.CEILING_PRICE, x.GST_RATE, x.EFFECTIVE_FROM, x.SO_NUMBER, x.PARA
    FROM (
        SELECT c.FORM_ID, c.CEILING_PRICE, c.GST_RATE, c.EFFECTIVE_FROM, c.SO_NUMBER, c.PARA,
               ROW_NUMBER() OVER (PARTITION BY c.FORM_ID
                                  ORDER BY c.EFFECTIVE_FROM DESC, c.SO_NUMBER DESC) AS RN
        FROM FF_REF_CEILING_PRICE c
        JOIN FF_V_ASOF a ON 1 = 1
        WHERE c.EFFECTIVE_FROM <= a.AS_OF
    ) x
    WHERE x.RN = 1
) cp ON cp.FORM_ID = f.FORM_ID
LEFT JOIN (
    SELECT FORM_ID, COUNT(DISTINCT MANUFACTURER_ID) AS PRODUCER_COUNT_MIN
    FROM FF_REF_PRODUCER
    GROUP BY FORM_ID
) pc ON pc.FORM_ID = f.FORM_ID
LEFT JOIN (
    SELECT FORM_ID, MIN(DAYS_OF_COVER) AS MIN_DAYS_OF_COVER
    FROM FF_V_DAYS_OF_COVER
    WHERE FORM_ID IS NOT NULL
    GROUP BY FORM_ID
) dc ON dc.FORM_ID = f.FORM_ID
LEFT JOIN (
    SELECT y.RUN_ID, y.FORM_ID, y.STARTED_AT, y.EXIT_RISK_BAND, y.EXIT_RISK, y.CAUSE_CODE, y.CONFIDENCE
    FROM (
        SELECT fo.RUN_ID, fo.FORM_ID, ru.STARTED_AT, fo.EXIT_RISK_BAND, fo.EXIT_RISK, fo.CAUSE_CODE,
               fo.CONFIDENCE,
               ROW_NUMBER() OVER (PARTITION BY fo.FORM_ID
                                  ORDER BY ru.STARTED_AT DESC, fo.RUN_ID DESC) AS RN
        FROM FF_AG_FORECAST fo
        JOIN FF_AG_RUN ru ON ru.RUN_ID = fo.RUN_ID
    ) y
    WHERE y.RN = 1
) fc ON fc.FORM_ID = f.FORM_ID
LEFT JOIN FF_AG_SIGNAL sg ON sg.RUN_ID = fc.RUN_ID AND sg.FORM_ID = f.FORM_ID
LEFT JOIN FF_AG_DEPENDENCY dp ON dp.RUN_ID = fc.RUN_ID AND dp.FORM_ID = f.FORM_ID;
