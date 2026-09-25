-- WHI-1445 cost-calibration Q2 (transaction samples) for mantle-101082044-cost-v1
-- window [2026-09-18 00:00:00, 2026-09-25 00:00:00) UTC
-- the window ends no later than snapshot block 101082044's timestamp
-- sample: h = xxhash64('WHI-1445/cost/v1|' || tx hash hex); sampled iff h & 7 = 0
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
s AS (
  SELECT * FROM j WHERE bitwise_and(from_big_endian_64(h), 7) = 0
),
gf AS (
  SELECT tx_hash, tx_fee_raw, gas_used AS gf_gas_used, gas_price AS gf_gas_price
  FROM gas.fees
  WHERE blockchain = 'mantle'
    AND block_month IN (DATE '2026-09-01')
    AND block_time >= TIMESTAMP '2026-09-18 00:00:00' AND block_time < TIMESTAMP '2026-09-25 00:00:00'
)
SELECT
  substr(lower(to_hex(s.h)), 1, 8) AS sample_key,
  s.tx_hash,
  s.block_number,
  s.tx_to,
  s.success,
  s.legs,
  s.evts,
  s.c,
  s.l,
  s.p,
  s.o,
  s.pools,
  s.pc,
  s.pl,
  s.pp,
  s.sold,
  s.bought,
  s.tokens,
  s.sources,
  s.sinks,
  s.gas_used,
  CAST(s.gas_price AS VARCHAR) AS gas_price,
  CAST(s.l1_fee AS VARCHAR) AS l1_fee,
  CAST(gf.tx_fee_raw AS VARCHAR) AS gasfees_tx_fee_raw,
  gf.gf_gas_used = s.gas_used AND gf.gf_gas_price = s.gas_price AS gasfees_same_gas
FROM s
LEFT JOIN gf ON gf.tx_hash = s.tx_hash
ORDER BY sample_key, s.tx_hash
