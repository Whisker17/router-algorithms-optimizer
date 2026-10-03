-- WHI-1436 corpus Q2 (strata samples) for mantle-5src-101082044
-- window [2026-09-11 00:00:00, 2026-09-25 00:00:00) UTC; seed 'WHI-1436/corpus/v1'
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
units (unit_kind, unit_in, unit_out) AS (
  VALUES
    ('pair', 0x779ded0c9e1022225f8e0630b35a9b54be713736, 0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8),
    ('pair', 0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8, 0x779ded0c9e1022225f8e0630b35a9b54be713736),
    ('pair', 0xcda86a272531e8640cd7f1a92c01839911b90bb0, 0xdeaddeaddeaddeaddeaddeaddeaddeaddead1111),
    ('pair', 0xdeaddeaddeaddeaddeaddeaddeaddeaddead1111, 0xcda86a272531e8640cd7f1a92c01839911b90bb0),
    ('pair', 0x779ded0c9e1022225f8e0630b35a9b54be713736, 0xcda86a272531e8640cd7f1a92c01839911b90bb0),
    ('pair', 0xcda86a272531e8640cd7f1a92c01839911b90bb0, 0x779ded0c9e1022225f8e0630b35a9b54be713736),
    ('pair', 0x201eba5cc46d216ce6dc03f6a759e8e766e956ae, 0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8),
    ('pair', 0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8, 0x201eba5cc46d216ce6dc03f6a759e8e766e956ae),
    ('pair', 0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8, 0xdeaddeaddeaddeaddeaddeaddeaddeaddead1111),
    ('pair', 0xdeaddeaddeaddeaddeaddeaddeaddeaddead1111, 0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8),
    ('pair', 0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9, 0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8),
    ('pair', 0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8, 0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9),
    ('pair', 0x779ded0c9e1022225f8e0630b35a9b54be713736, 0xc96de26018a54d51c097160568752c4e3bd6c364),
    ('pair', 0xc96de26018a54d51c097160568752c4e3bd6c364, 0x779ded0c9e1022225f8e0630b35a9b54be713736),
    ('pair', 0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9, 0x779ded0c9e1022225f8e0630b35a9b54be713736),
    ('pair', 0x779ded0c9e1022225f8e0630b35a9b54be713736, 0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9),
    ('pair', 0x201eba5cc46d216ce6dc03f6a759e8e766e956ae, 0x779ded0c9e1022225f8e0630b35a9b54be713736),
    ('pair', 0x779ded0c9e1022225f8e0630b35a9b54be713736, 0x201eba5cc46d216ce6dc03f6a759e8e766e956ae),
    ('pair', 0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9, 0x201eba5cc46d216ce6dc03f6a759e8e766e956ae),
    ('pair', 0x201eba5cc46d216ce6dc03f6a759e8e766e956ae, 0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9),
    ('pair', 0x201eba5cc46d216ce6dc03f6a759e8e766e956ae, 0xcda86a272531e8640cd7f1a92c01839911b90bb0),
    ('pair', 0xcda86a272531e8640cd7f1a92c01839911b90bb0, 0x201eba5cc46d216ce6dc03f6a759e8e766e956ae),
    ('pair', 0x1bdd8878252daddd3af2ba30628813271294edc0, 0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8),
    ('pair', 0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8, 0x1bdd8878252daddd3af2ba30628813271294edc0),
    ('token', 0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9, CAST(NULL AS VARBINARY)),
    ('token', 0x1bdd8878252daddd3af2ba30628813271294edc0, CAST(NULL AS VARBINARY)),
    ('token', 0x201eba5cc46d216ce6dc03f6a759e8e766e956ae, CAST(NULL AS VARBINARY)),
    ('token', 0x779ded0c9e1022225f8e0630b35a9b54be713736, CAST(NULL AS VARBINARY)),
    ('token', 0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8, CAST(NULL AS VARBINARY)),
    ('token', 0xc96de26018a54d51c097160568752c4e3bd6c364, CAST(NULL AS VARBINARY)),
    ('token', 0xcda86a272531e8640cd7f1a92c01839911b90bb0, CAST(NULL AS VARBINARY)),
    ('token', 0xdeaddeaddeaddeaddeaddeaddeaddeaddead1111, CAST(NULL AS VARBINARY))
),
unit_legs AS (
  SELECT u.unit_kind, u.unit_in, u.unit_out, l.*
  FROM legs l
  JOIN units u
    ON l.token_in = u.unit_in
   AND (u.unit_out IS NULL OR l.token_out = u.unit_out)
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY unit_kind, unit_in, unit_out ORDER BY amount_in_raw, tx_hash, evt_index
    ) AS amount_rank,
    COUNT(*) OVER (PARTITION BY unit_kind, unit_in, unit_out) AS unit_legs
  FROM unit_legs
),
stratified AS (
  SELECT
    *,
    CASE
      WHEN amount_rank - 1 >= 0.05 * unit_legs AND amount_rank - 1 < 0.4 * unit_legs THEN 'low'
      WHEN amount_rank - 1 >= 0.4 * unit_legs AND amount_rank - 1 < 0.8 * unit_legs THEN 'medium'
      WHEN amount_rank - 1 >= 0.8 * unit_legs AND amount_rank - 1 < 0.99 * unit_legs THEN 'large'
    END AS stratum
  FROM ranked
),
banded AS (
  SELECT
    *,
    COUNT(*) OVER (PARTITION BY unit_kind, unit_in, unit_out, stratum) AS stratum_legs,
    MIN(amount_in_raw) OVER (PARTITION BY unit_kind, unit_in, unit_out, stratum) AS stratum_min_raw,
    MAX(amount_in_raw) OVER (PARTITION BY unit_kind, unit_in, unit_out, stratum) AS stratum_max_raw,
    ROW_NUMBER() OVER (
      PARTITION BY unit_kind, unit_in, unit_out, stratum, tx_hash ORDER BY evt_index
    ) AS tx_leg
  FROM stratified
  WHERE stratum IS NOT NULL
),
sampled AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY unit_kind, unit_in, unit_out, stratum
      ORDER BY xxhash64(to_utf8(concat(
        'WHI-1436/corpus/v1', '|', lower(to_hex(tx_hash)), '|', CAST(evt_index AS VARCHAR)
      ))), tx_hash, evt_index
    ) AS sample_rank
  FROM banded
  WHERE tx_leg = 1
)
SELECT
  unit_kind,
  unit_in,
  unit_out,
  stratum,
  sample_rank,
  unit_legs,
  stratum_legs,
  CAST(stratum_min_raw AS VARCHAR) AS stratum_min_raw,
  CAST(stratum_max_raw AS VARCHAR) AS stratum_max_raw,
  amount_rank,
  source_key,
  token_out AS leg_token_out,
  CAST(amount_in_raw AS VARCHAR) AS amount_in_raw,
  amount_usd,
  tx_hash,
  evt_index,
  block_number,
  block_time
FROM sampled
WHERE sample_rank <= CASE unit_kind
    WHEN 'pair' THEN 4
    ELSE 2
  END
ORDER BY unit_kind, unit_in, unit_out, stratum, sample_rank
