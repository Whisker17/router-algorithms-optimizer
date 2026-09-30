# SPDX-License-Identifier: MIT
# Validation-only author-reference run for WHI-1557 (cfmm_dual contract).
#
# Loads the pinned, unmodified CFMMRouter.jl (bcc-research/CFMMRouter.jl @
# 5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267, MIT; see NOTICE.md) through this directory's
# Manifest.toml, runs it OFFLINE on tests/fixtures/cfmm/author_inputs.json and writes
# tests/fixtures/cfmm/author_reference.json. It is never timed and never used by the
# Python benchmark at runtime. `--check` regenerates in memory and fails unless the
# committed file is byte-identical.
#
#   JULIA_NUM_THREADS=1 julia --project=tools/upstream/cfmm tools/upstream/cfmm/generate.jl [--check]

using CFMMRouter
using JSON3
using LinearAlgebra
using Pkg

const CR = CFMMRouter
const ROOT = normpath(joinpath(@__DIR__, "..", "..", ".."))
const INPUTS = joinpath(ROOT, "tests", "fixtures", "cfmm", "author_inputs.json")
const OUTPUT = joinpath(ROOT, "tests", "fixtures", "cfmm", "author_reference.json")
const PIN = "5932e42e5077ffc7d8e02c3b3ad2e9ed1d441267"

gamma(fee_bps) = (10000 - fee_bps) / 10000

function cpmm_oracle(c)
    pool = ProductTwoCoin(Float64.(collect(c.R)), gamma(c.fee_bps), [1, 2])
    Δ, Λ = zeros(2), zeros(2)
    find_arb!(Δ, Λ, pool, Float64.(collect(c.v)))
    return (id = String(c.id), delta = Δ, lambda = Λ)
end

function univ3_oracle(c)
    pool = UniV3(Float64(c.current_price), Float64.(collect(c.lower_ticks)),
                 Float64.(collect(c.liquidity)), Float64(c.gamma), [1, 2])
    Δ, Λ = zeros(2), zeros(2)
    find_arb!(Δ, Λ, pool, Float64.(collect(c.v)))
    return (id = String(c.id), current_tick = pool.current_tick, delta = Δ, lambda = Λ)
end

function router_case(c)
    tokens = String.(collect(c.tokens))
    n = length(tokens)
    idx = Dict(t => i for (i, t) in enumerate(tokens))
    cfmms = CFMM{Float64}[
        ProductTwoCoin([Float64(p.reserve0), Float64(p.reserve1)], gamma(p.fee_bps),
                       [idx[String(p.token0)], idx[String(p.token1)]])
        for p in c.pools
    ]
    objective = Swap(idx[String(c.token_out)], idx[String(c.token_in)], Float64(c.amount_in), n)
    router = Router(objective, cfmms, n)
    kwargs = Dict(Symbol(k) => v for (k, v) in pairs(c.route_kwargs))
    returned = route!(router; kwargs...)
    v = copy(router.v)
    trades = [
        (pool_id = String(p.pool_id), delta = collect(Δ), lambda = collect(Λ))
        for (p, Δ, Λ) in zip(c.pools, router.Δs, router.Λs)
    ]
    arb = sum(dot(Λ, v[m.Ai]) - dot(Δ, v[m.Ai]) for (Δ, Λ, m) in zip(router.Δs, router.Λs, router.cfmms))
    return (
        id = String(c.id),
        route_returned = repr(returned),
        v = v,
        netflows = netflows(router),
        trades = trades,
        conjugate = CR.f(objective, v),
        dual_value = CR.f(objective, v) + arb,
    )
end

function provenance()
    deps = Pkg.dependencies()
    info(name) = only(d for d in values(deps) if d.name == name)
    cfmm = info("CFMMRouter")
    cfmm.git_revision == PIN || error("CFMMRouter revision $(cfmm.git_revision) is not the pin $PIN")
    Threads.nthreads() == 1 || error("run with JULIA_NUM_THREADS=1 (author find_arb! uses Threads.@threads)")
    return (
        generator = "tools/upstream/cfmm/generate.jl",
        inputs = "tests/fixtures/cfmm/author_inputs.json",
        julia = string(VERSION),
        threads = Threads.nthreads(),
        cfmmrouter = (version = string(cfmm.version), git_revision = cfmm.git_revision,
                      repo = cfmm.git_source),
        lbfgsb = string(info("LBFGSB").version),
        l_bfgs_b_jll = string(info("L_BFGS_B_jll").version),
        route_defaults = "route!(r; v=nothing, verbose=false, m=5, factr=1e1, pgtol=1e-5, maxfun=15_000, maxiter=15_000)",
    )
end

function generate()
    inp = JSON3.read(read(INPUTS, String))
    doc = (
        _comment = "Author-reference outputs of the pinned CFMMRouter.jl (WHI-1557). Generated OFFLINE by " *
                   "tools/upstream/cfmm/regen.sh; not a timed solver. Floats are the author's Float64 values.",
        provenance = provenance(),
        cpmm_oracle = [cpmm_oracle(c) for c in inp.cpmm_oracle],
        univ3_oracle = [univ3_oracle(c) for c in inp.univ3_oracle],
        router = [router_case(c) for c in inp.router],
    )
    io = IOBuffer()
    JSON3.pretty(io, JSON3.write(doc))
    println(io)
    return String(take!(io))
end

text = generate()
if "--check" in ARGS
    read(OUTPUT, String) == text || error("$(OUTPUT) is not reproduced byte-for-byte")
    println("check ok: $(OUTPUT)")
else
    write(OUTPUT, text)
    println("wrote $(OUTPUT)")
end
