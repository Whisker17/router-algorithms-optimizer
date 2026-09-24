#!/usr/bin/env bash
# Reproduce the WHI-1428 source diff (docs/references/concentrated-liquidity-migration.md §2):
# fetch the explorer-verified pool sources for the three Mantle CL example pools from
# Routescan's keyless Etherscan-compatible API, check the Uniswap pool against
# Uniswap/v3-core v1.0.0, and print the semantic (comment/whitespace/pragma/import-
# insensitive) differences of the libraries and of AgniPool/FusionXV3Pool vs
# UniswapV3Pool. Network-touching; not part of the offline test suite.
set -euo pipefail
WORK="${1:-$(mktemp -d)}"
API="https://api.routescan.io/v2/network/mainnet/evm/5000/etherscan/api"
mkdir -p "$WORK" && cd "$WORK"
echo "work dir: $WORK"

fetch() { curl -fsS "$API?module=contract&action=getsourcecode&address=$2" -o "$1.json"; }
fetch uni 0x4cdFc22bF05209de87Ee564746Dc7E5174631d2b
fetch agni 0x1858d52cf57c07a018171d7a1e68dc081f17144f
fetch fusionx 0x262255f4770aebe2d0c8b97a46287dcecc2a0aff
[ -d v3core ] || git clone -q --depth 1 --branch v1.0.0 https://github.com/Uniswap/v3-core.git v3core

python3 - <<'EOF'
import json, os, re, subprocess

def explode(name):
    d = json.load(open(f"{name}.json"))["result"][0]
    print(f"{name}: ContractName={d['ContractName']} compiler={d['CompilerVersion']} runs={d['Runs']}")
    s = d["SourceCode"]
    raw = json.loads(s[1:-1])["sources"] if s.startswith("{{") else {"flat.sol": {"content": s}}
    files = {}
    for k, v in raw.items():
        content = v["content"].replace("\r\n", "\n")
        if "\n// File " not in content:
            files[k] = content
            continue
        # hardhat-flattened single file: split on the "// File <path>" markers
        for part in re.split(r"\n// File ", content)[1:]:
            header, body = part.split("\n", 1)
            header = header.strip()
            key = header if not header.startswith("@") else "lm/" + header.split("/contracts/")[1].split("@")[0]
            files[key] = body
    return files

def norm(src):
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(pragma|import)[^;]*;", "", src)
    return re.sub(r"\s+", "", src)

uni, agni, fx = explode("uni"), explode("agni"), explode("fusionx")
libs = sorted(k for k in uni if k.startswith("contracts/libraries/"))

v3 = {k: open(os.path.join("v3core", k)).read() for k in uni if os.path.exists(os.path.join("v3core", k))}
bad = [k for k in uni if k in v3 and norm(uni[k]) != norm(v3[k])]
print(f"\nUniswap verified source vs v3-core v1.0.0: {len(v3)} files compared, semantic diffs: {bad or 'none'}")
for other_name, other in (("agni", agni), ("fusionx", fx)):
    diffs = [k for k in libs if k in other and norm(other[k]) != norm(uni[k])]
    missing = [k for k in libs if k not in other]
    print(f"{other_name} libraries vs Uniswap: semantic diffs: {diffs or 'none'}; missing: {missing or 'none'}")

def body(files, key):
    return next(v for k, v in files.items() if k.endswith("/" + key) or k == key)

def canon(src, prefix):
    # neutralize the fork's naming (UniswapV3Pool/AgniPool/FusionXV3Pool -> XPool,
    # uniswapV3SwapCallback/agniSwapCallback/fusionXV3SwapCallback -> xSwapCallback)
    src = src.replace(prefix, "X").replace(prefix[0].lower() + prefix[1:], "x")
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(pragma|import)[^;]*;", "", src)
    return "\n".join(l.strip() for l in src.splitlines() if l.strip()) + "\n"

open("uni_pool.sol", "w").write(canon(body(uni, "UniswapV3Pool.sol"), "UniswapV3"))
open("agni_pool.sol", "w").write(canon(body(agni, "AgniPool.sol"), "Agni"))
open("fx_pool.sol", "w").write(canon(body(fx, "FusionXV3Pool.sol"), "FusionXV3"))
for a, b in (("uni_pool.sol", "agni_pool.sol"), ("agni_pool.sol", "fx_pool.sol")):
    out = subprocess.run(["diff", a, b], capture_output=True, text=True).stdout
    print(f"\n===== diff {a} {b} ({'identical' if not out else str(out.count(chr(10))) + ' lines'})")
    print(out)
EOF
