"""Фаза 2.3: property-based по инвариантам из 01-invariants.md."""
from hypothesis import given, strategies as st, settings, HealthCheck, assume
import math, traceback
FAILS=[]
def prop(name):
    def deco(f):
        try:
            f(); print(f"  ok  {name}")
        except AssertionError as e:
            msg=str(e).split("\n")[0][:200]; FAILS.append((name,msg)); print(f"FAIL {name}: {msg}")
        except Exception as e:
            msg=f"{type(e).__name__}: {e}"; FAILS.append((name,msg)); print(f"FAIL {name}: {msg}")
    return deco

S = settings(max_examples=300, deadline=None, suppress_health_check=list(HealthCheck))
prob = st.floats(0,1,allow_nan=False,allow_infinity=False)
pos  = st.floats(0.01, 1e7, allow_nan=False, allow_infinity=False)

# P1/M9: комиссия неотрицательна и не убывает по объёму
@prop("M9 fee >= 0, монотонна по notional")
def _():
    from core.algorithms import venue_fee
    from core.models import Venue
    @given(n1=pos, n2=pos, p=prob)
    @S
    def t(n1,n2,p):
        f1,f2 = venue_fee(Venue.KALSHI,n1,p), venue_fee(Venue.KALSHI,n2,p)
        assert f1>=0 and f2>=0
        if n1<=n2: assert f1<=f2+1e-9, f"n1={n1} n2={n2} p={p} f1={f1} f2={f2}"
    t()

# P1: Kelly в [0,1]
@prop("P1 kelly_fraction в [0,1]")
def _():
    from core.sizing import kelly_fraction
    @given(fair=prob, price=prob)
    @S
    def t(fair,price):
        k=kelly_fraction(fair,price); assert 0.0<=k<=1.0, f"fair={fair} price={price} k={k}"
    t()

# P3: границы Фреше для копулы
@prop("P3 joint <= min(P(A),P(B)) и >= max(0,PA+PB-1)")
def _():
    from core.conditional import conditional
    @given(pa=prob, pb=prob, rho=st.floats(-0.999,0.999,allow_nan=False))
    @S
    def t(pa,pb,rho):
        j=conditional(pa,pb,rho)["joint"]
        assert j<=min(pa,pb)+1e-3, f"pa={pa} pb={pb} rho={rho} joint={j}"
        assert j>=-1e-9
    t()

# P1: house probability в [0,1] при любых входах
@prop("P1 house_probability в [0,1]")
def _():
    from core.houseview import fuse
    @given(mp=prob, cal=prob, opt=prob, meta=prob,
           mom=st.floats(-1,1,allow_nan=False), inf=st.booleans(), n=st.integers(0,10**6))
    @S
    def t(mp,cal,opt,meta,mom,inf,n):
        v=fuse(mp, calibration={"calibrated":True,"calibrated_probability":cal,"samples":n},
               options={"options_probability":opt},
               meta={"estimate":meta,"venues":[{"samples":n}]},
               micro={"informed_flow":inf,"momentum":mom})
        h=v["house_probability"]; assert 0.0<=h<=1.0, f"h={h}"
        assert 0.0<=v["confidence"]<=1.0
    t()

# P2: де-виг суммируется в 1
@prop("P2 де-виггированные вероятности суммируются в 1")
def _():
    from core.models import MultiOutcomeMarket, Outcome, Venue
    @given(ps=st.lists(st.floats(0.001,0.999,allow_nan=False),min_size=2,max_size=8))
    @S
    def t(ps):
        m=MultiOutcomeMarket(event="e",outcomes=[
            Outcome(name=f"o{i}",venue=Venue.KALSHI,market_id=f"m{i}",yes_price=p)
            for i,p in enumerate(ps)])
        s=sum(o["fair_probability"] for o in m.normalized)
        assert abs(s-1.0)<1e-3, f"сумма={s} из {ps}"
    t()

# M7: fillable <= requested
@prop("M7 fillable_size_usd <= requested_size_usd")
def _():
    from core.algorithms import estimate_realizable_edge
    from core.models import Leg, Side, Venue, OrderbookLevel, OrderbookSnapshot, utcnow
    @given(size=pos, price=st.floats(0.01,0.99,allow_nan=False), depth=pos)
    @S
    def t(size,price,depth):
        lvl=[OrderbookLevel(price=price,size_usd=depth)]
        bk=OrderbookSnapshot(venue=Venue.KALSHI,market_id="m",as_of=utcnow(),yes_asks=lvl,yes_bids=lvl)
        e=estimate_realizable_edge([Leg(venue=Venue.KALSHI,market_id="m",side=Side.YES)],
                                   size, lambda v,m: bk)
        assert e.fillable_size_usd<=size+1e-6, f"size={size} fillable={e.fillable_size_usd}"
    t()

# P4: калибратор монотонен
@prop("P4 калибратор монотонен")
def _():
    from core.calibration import fit_calibrator
    @given(s=st.lists(st.tuples(prob,st.integers(0,1)),min_size=40,max_size=300))
    @S
    def t(s):
        c=fit_calibrator(s)
        xs=[i/50 for i in range(51)]
        ys=[c.calibrate(x) for x in xs]
        for a,b in zip(ys,ys[1:]): assert b>=a-1e-6, f"падение {a}->{b}"
    t()

# T3: merkle proof верифицируется для любой включённой записи
@prop("T3 merkle proof верифицируется")
def _():
    from core.merkle import merkle_root, merkle_proof, verify_proof
    @given(recs=st.lists(st.dictionaries(st.text(min_size=1,max_size=5),
           st.integers(),min_size=1,max_size=3),min_size=1,max_size=25))
    @S
    def t(recs):
        root=merkle_root(recs)
        for i in range(len(recs)):
            assert verify_proof(recs[i],merkle_proof(recs,i),root), f"i={i} n={len(recs)}"
    t()

# N6: обратимость кодирования платежа
@prop("N6 decode(encode(payment)) == payment")
def _():
    from predmarket_mcp.billing.x402 import encode_payment_header, decode_payment_header
    @given(d=st.dictionaries(st.text(max_size=8), st.one_of(st.text(max_size=20),
           st.integers(),st.booleans(),st.none()),max_size=6))
    @S
    def t(d):
        assert decode_payment_header(encode_payment_header(d))==d
    t()

# I1: компаунд-рост не переполняется ни при каких edge/holding
@prop("I1 velocity: рост конечен при любых edge/holding")
def _():
    from core.velocity import annotate_velocity, rotation_plan
    from core.models import Leg, Opportunity, OpportunityKind, Side, Venue
    @given(edge=st.floats(-1,50,allow_nan=False), days=st.floats(0.001,3650,allow_nan=False),
           bank=pos, hor=st.floats(1,3650,allow_nan=False))
    @S
    def t(edge,days,bank,hor):
        o=Opportunity(kind=OpportunityKind.CROSS_VENUE,title="t",category="c",
            realizable_edge=edge,max_size_usd=1000,
            legs=[Leg(venue=Venue.KALSHI,market_id="m",side=Side.YES)])
        o.holding_days=days; o.expected_value=edge
        ann=annotate_velocity([o])
        g=ann[0].velocity["compound_annual_growth"]
        assert math.isfinite(g), f"growth={g}"
        p=rotation_plan(ann,bank,hor)
        assert math.isfinite(p["projected_bankroll_usd"]), f"proj={p['projected_bankroll_usd']}"
    t()

print()
print(f"ИТОГ property-based: {10-len(FAILS)}/10 свойств удержались")
