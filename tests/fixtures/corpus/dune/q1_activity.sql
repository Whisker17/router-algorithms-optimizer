-- WHI-1436 corpus Q1 (activity) for mantle-5src-101082044
-- window [2026-09-11 00:00:00, 2026-09-25 00:00:00) UTC; end == snapshot block timestamp
WITH legs AS (
  SELECT
    CASE
      WHEN project = 'agni' AND version = '3' THEN 'agni_v3'
      WHEN project = 'fusionx' AND version = '3' THEN 'fusionx_v3'
      WHEN project = 'uniswap' AND version = '3' THEN 'uniswap_v3'
      WHEN project = 'merchant_moe' AND version = '1' THEN 'moe_classic_v1'
      WHEN project = 'merchant_moe' AND version = '2.2' THEN 'moe_lb_v2_2'
    END AS source_key,
    token_sold_address AS token_in,
    token_bought_address AS token_out,
    token_sold_amount_raw AS amount_in_raw,
    amount_usd,
    tx_hash,
    evt_index,
    block_time,
    block_number
  FROM dex.trades
  WHERE blockchain = 'mantle'
    AND block_month IN (DATE '2026-09-01')
    AND block_time >= TIMESTAMP '2026-09-11 00:00:00'
    AND block_time < TIMESTAMP '2026-09-25 00:00:00'
    AND ((project = 'agni' AND version = '3')
     OR (project = 'fusionx' AND version = '3')
     OR (project = 'uniswap' AND version = '3')
     OR (project = 'merchant_moe' AND version = '1')
     OR (project = 'merchant_moe' AND version = '2.2'))
    AND token_sold_address <> token_bought_address
    AND token_sold_amount_raw > UINT256 '0'
),
pair_rows AS (
  SELECT
    'pair' AS kind,
    LEAST(token_in, token_out) AS token_a,
    GREATEST(token_in, token_out) AS token_b,
    COUNT(*) AS legs,
    COUNT(DISTINCT tx_hash) AS distinct_txs,
    SUM(amount_usd) AS priced_usd,
    COUNT_IF(amount_usd IS NULL) AS unpriced_legs,
  COUNT_IF(source_key = 'agni_v3') AS legs_agni_v3,
  COUNT_IF(source_key = 'fusionx_v3') AS legs_fusionx_v3,
  COUNT_IF(source_key = 'uniswap_v3') AS legs_uniswap_v3,
  COUNT_IF(source_key = 'moe_classic_v1') AS legs_moe_classic_v1,
  COUNT_IF(source_key = 'moe_lb_v2_2') AS legs_moe_lb_v2_2
  FROM legs
  GROUP BY 2, 3
  HAVING COUNT(DISTINCT tx_hash) >= 5
),
token_legs AS (
  SELECT token_in AS token, tx_hash, amount_usd, source_key FROM legs
  UNION ALL
  SELECT token_out AS token, tx_hash, amount_usd, source_key FROM legs
),
token_rows AS (
  SELECT
    'token' AS kind,
    token AS token_a,
    CAST(NULL AS VARBINARY) AS token_b,
    COUNT(*) AS legs,
    COUNT(DISTINCT tx_hash) AS distinct_txs,
    SUM(amount_usd) AS priced_usd,
    COUNT_IF(amount_usd IS NULL) AS unpriced_legs,
  COUNT_IF(source_key = 'agni_v3') AS legs_agni_v3,
  COUNT_IF(source_key = 'fusionx_v3') AS legs_fusionx_v3,
  COUNT_IF(source_key = 'uniswap_v3') AS legs_uniswap_v3,
  COUNT_IF(source_key = 'moe_classic_v1') AS legs_moe_classic_v1,
  COUNT_IF(source_key = 'moe_lb_v2_2') AS legs_moe_lb_v2_2
  FROM token_legs
  GROUP BY 2
  HAVING COUNT(DISTINCT tx_hash) >= 50
)
SELECT * FROM pair_rows
UNION ALL
SELECT * FROM token_rows
ORDER BY kind, distinct_txs DESC, token_a, token_b
