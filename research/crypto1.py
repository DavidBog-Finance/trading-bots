from lib import *
d=load_btc(); c=d.close; ret=c.pct_change().fillna(0); C=0.003; PPY=365
IS=('2013','2020'); OOS=('2021','2026')
def ev(pos,lag=1,cost=C):
    r,t=run(pos,ret,cost,None,lag); w=pos.shift(lag).fillna(0)
    return stats(r[IS[0]:IS[1]],t,PPY,w), stats(r[OOS[0]:OOS[1]],t,PPY,w)
def p(name,pos,**k):
    a,b=ev(pos,**k); print(f"{name:24s} IS  {fmt(a)}\n{'':24s} OOS {fmt(b)}")
p('BuyHold',pd.Series(1.0,index=c.index))
print('== SMA grid: IS Sharpe | OOS Sharpe | OOS CAGR | OOS MaxDD | trades/yr')
for n in [10,20,30,50,75,100,150,200]:
    a,b=ev((c>c.rolling(n).mean()).astype(float))
    print(n,f"{a['Sharpe']:.2f} | {b['Sharpe']:.2f} | {b['CAGR']:.1%} | {b['MaxDD']:.1%} | {b['Trades/yr']:.1f}")
print('== TSMOM lookback (ret over L days >0)')
for L in [7,14,28,56,90,120,180]:
    a,b=ev((c.pct_change(L)>0).astype(float))
    print(L,f"{a['Sharpe']:.2f} | {b['Sharpe']:.2f} | {b['CAGR']:.1%} | {b['MaxDD']:.1%} | {b['Trades/yr']:.1f}")
print('== Donchian entry N-day high, exit M-day low')
for N in [20,40,55]:
    for M in [10,20]:
        hi=d.high.rolling(N).max().shift(1); lo=d.low.rolling(M).min().shift(1)
        pos=pd.Series(np.nan,index=c.index); pos[c>hi]=1; pos[c<lo]=0; pos=pos.ffill().fillna(0)
        a,b=ev(pos); print(N,M,f"{a['Sharpe']:.2f} | {b['Sharpe']:.2f} | {b['CAGR']:.1%} | {b['MaxDD']:.1%} | {b['Trades/yr']:.1f}")
