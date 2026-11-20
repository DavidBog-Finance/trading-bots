from lib import *
d=load_spx(); ret=d.tr.pct_change().fillna(0); c=d.close; rf=d.rf; C=0.0005
IS=('1928','1999'); OOS=('2000','2025')
def ev(pos,lag=1):
    r,t=run(pos,ret,C,rf,lag); w=pos.shift(lag).fillna(0)
    return stats(r[IS[0]:IS[1]],t,w=w), stats(r[OOS[0]:OOS[1]],t,w=w)
def rsi2(entry=10, exitma=5, trend=200, n=2):
    r=rsi(c,n).values; sv=c.rolling(trend).mean().values; mv=c.rolling(exitma).mean().values; cv=c.values
    pos=np.zeros(len(c)); inpos=False
    for i in range(len(c)):
        if not inpos and cv[i]>sv[i] and r[i]<entry: inpos=True
        elif inpos and cv[i]>mv[i]: inpos=False
        pos[i]=inpos
    return pd.Series(pos,index=c.index)
print("== SMA trend grid (IS Sharpe / OOS Sharpe / OOS CAGR / OOS MaxDD)")
for n in [50,100,150,200,250,300]:
    a,b=ev((c>c.rolling(n).mean()).astype(float))
    print(n, f"{a['Sharpe']:.2f} {b['Sharpe']:.2f} {b['CAGR']:.2%} {b['MaxDD']:.1%} tr/yr {b['Trades/yr']:.1f}")
print("== SMA with 1% band (enter >1.01*SMA, exit <0.99*SMA)")
for n in [100,150,200,250]:
    s=c.rolling(n).mean(); pos=pd.Series(np.nan,index=c.index); pos[c>s*1.01]=1; pos[c<s*0.99]=0; pos=pos.ffill().fillna(0)
    a,b=ev(pos); print(n, f"{a['Sharpe']:.2f} {b['Sharpe']:.2f} {b['CAGR']:.2%} {b['MaxDD']:.1%} tr/yr {b['Trades/yr']:.1f}")
print("== RSI2 grid: entry x exitMA (IS Sharpe | OOS Sharpe)")
for e in [5,10,15,20,25,30]:
    row=[]
    for x in [3,5,10]:
        a,b=ev(rsi2(e,x)); row.append(f"{a['Sharpe']:.2f}|{b['Sharpe']:.2f}")
    print(e,row)
print("== Vol targeting (no leverage) w=min(1,tv/rv20)")
rv=ret.rolling(20).std()*np.sqrt(252)
for tv in [0.10,0.12,0.15,0.20]:
    a,b=ev((tv/rv).clip(upper=1).fillna(0)); print(tv,f"IS {fmt(a)}\n     OOS {fmt(b)}")
print("== Turn of month: last day + first 3 days")
idx=c.index; m=pd.Series(idx.to_period('M'),index=idx)
rank=m.groupby(m).cumcount(); rrank=m.groupby(m).cumcount(ascending=False)
# position decided at close t applies to t+1 return. Want to earn returns on day -1 and days 1..3 -> hold at close of day -2 through close of day 3
pos=((rrank<=1)|(rank<=1)).astype(float)
a,b=ev(pos); print('TOM IS',fmt(a)); print('TOM OOS',fmt(b))
