-- WHI-1436 corpus Q3 (frozen price context) for mantle-5src-101082044
-- latest prices.minute minute bucket [t, t+60s) with
-- snapshot - 3600s <= t and t + 60s <= snapshot = 1790294400
WITH universe (token) AS (
  VALUES
    (0x09bc4e0d864854c6afb6eb9a9cdf58ac190d0df9),
    (0x1bdd8878252daddd3af2ba30628813271294edc0),
    (0x201eba5cc46d216ce6dc03f6a759e8e766e956ae),
    (0x779ded0c9e1022225f8e0630b35a9b54be713736),
    (0x78c1b0c915c4faa5fffa6cabf0219da63d7f4cb8),
    (0xc96de26018a54d51c097160568752c4e3bd6c364),
    (0xcda86a272531e8640cd7f1a92c01839911b90bb0),
    (0xdeaddeaddeaddeaddeaddeaddeaddeaddead1111)
),
candidates AS (
  SELECT
    p.contract_address AS token,
    p.timestamp,
    p.price,
    p.decimals,
    p.symbol,
    p.source,
    ROW_NUMBER() OVER (PARTITION BY p.contract_address ORDER BY p.timestamp DESC) AS rn
  FROM prices.minute p
  JOIN universe u ON p.contract_address = u.token
  WHERE p.blockchain = 'mantle'
    AND p.timestamp >= from_unixtime(1790290800)
    AND p.timestamp <= from_unixtime(1790294340)
)
SELECT
  u.token,
  to_unixtime(c.timestamp) AS price_timestamp,
  c.price,
  c.decimals,
  c.symbol,
  c.source
FROM universe u
LEFT JOIN candidates c ON c.token = u.token AND c.rn = 1
ORDER BY u.token
