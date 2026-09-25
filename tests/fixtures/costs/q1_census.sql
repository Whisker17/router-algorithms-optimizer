-- WHI-1445 cost-calibration Q1 (window census) for mantle-101082044-cost-v1
-- window [2026-09-18 00:00:00, 2026-09-25 00:00:00) UTC
-- the window ends no later than snapshot block 101082044's timestamp
WITH legs AS (
  SELECT
    tx_hash,
    evt_index,
    CASE
      WHEN project = 'agni' AND version = '3' THEN 'c'
      WHEN project = 'fusionx' AND version = '3' THEN 'c'
      WHEN project = 'uniswap' AND version = '3' THEN 'c'
      WHEN project = 'merchant_moe' AND version = '2.2' THEN 'l'
      WHEN project = 'merchant_moe' AND version = '1' THEN 'p'
      ELSE 'o'
    END AS fam,
    project_contract_address AS pool,
    token_sold_address AS token_in,
    token_bought_address AS token_out
  FROM dex.trades
  WHERE blockchain = 'mantle'
    AND block_month IN (DATE '2026-09-01')
    AND block_time >= TIMESTAMP '2026-09-18 00:00:00' AND block_time < TIMESTAMP '2026-09-25 00:00:00'
),
tx_legs AS (
  SELECT
    tx_hash,
    COUNT(*) AS legs,
    COUNT(DISTINCT evt_index) AS evts,
    COUNT_IF(fam = 'c') AS c,
    COUNT_IF(fam = 'l') AS l,
    COUNT_IF(fam = 'p') AS p,
    COUNT_IF(fam = 'o') AS o,
    COUNT(DISTINCT pool) AS pools,
    COUNT(DISTINCT IF(fam = 'c', pool)) AS pc,
    COUNT(DISTINCT IF(fam = 'l', pool)) AS pl,
    COUNT(DISTINCT IF(fam = 'p', pool)) AS pp,
    COUNT(DISTINCT token_in) AS sold,
    COUNT(DISTINCT token_out) AS bought,
    cardinality(array_union(array_agg(token_in), array_agg(token_out))) AS tokens,
    cardinality(array_except(array_agg(token_in), array_agg(token_out))) AS sources,
    cardinality(array_except(array_agg(token_out), array_agg(token_in))) AS sinks
  FROM legs
  GROUP BY tx_hash
),
j AS (
  SELECT
    tl.*,
    tx."to" AS tx_to,
    tx.block_number,
    tx.gas_used,
    tx.gas_price,
    tx.l1_fee,
    tx.success,
    xxhash64(to_utf8(concat('WHI-1445/cost/v1|', lower(to_hex(tl.tx_hash))))) AS h
  FROM tx_legs tl
  LEFT JOIN mantle.transactions tx
    ON tx.hash = tl.tx_hash
   AND tx.block_date >= DATE '2026-09-18' AND tx.block_date < DATE '2026-09-25'
   AND tx.block_time >= TIMESTAMP '2026-09-18 00:00:00' AND tx.block_time < TIMESTAMP '2026-09-25 00:00:00'
),
shapes AS (
  SELECT
    'shape' AS kind,
    CAST(NULL AS VARBINARY) AS tx_to,
    c, l, p, o, pools, pc, pl, pp, sold, bought, tokens, sources, sinks,
    COUNT(*) AS txs,
    COUNT_IF(success = false) AS failed,
    COUNT_IF(gas_used IS NULL) AS missing,
    COUNT_IF(legs <> evts) AS duplicate_leg_rows,
    SUM(legs) AS legs
  FROM j
  GROUP BY c, l, p, o, pools, pc, pl, pp, sold, bought, tokens, sources, sinks
),
routers AS (
  SELECT
    'router' AS kind,
    tx_to,
    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    COUNT(*) AS txs,
    COUNT_IF(success = false) AS failed,
    0,
    0,
    SUM(legs) AS legs
  FROM j
  WHERE tx_to IS NOT NULL
  GROUP BY tx_to
  ORDER BY COUNT(*) DESC, tx_to
  LIMIT 24
)
SELECT * FROM shapes
UNION ALL
SELECT * FROM routers
ORDER BY kind, txs DESC, c, l, p, o, pools, pc, pl, pp, sold, bought, tokens, sources, sinks,
  tx_to
