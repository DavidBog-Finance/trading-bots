from lib import *
d=load_spx(); ret=d.tr.pct_change().fillna(0); c=d.close; rf=d.rf; C=0.0005
P={'IS 1928-99':('1928','1999'),'OOS 2000-25':('2000','2025'),'2008':('2008','2008'),'2022':('2022','2022'),'2020':('2020','2020')}
def ev(name,pos,lag=1):
    r,t=run(pos,ret,C,rf,lag); w=pos.shift(lag).fillna(0)
    for p,(a,b) in P.items():
        s=stats(r[a:b],t,w=w)
        if p in('2008','2022','2020'): print(f"   {p}: {s['CAGR']:.1%} maxDD {s['MaxDD']:.1%}",end='')
        else: print(f"\n{name:26s} {p:12s} {fmt(s)}",end='')
    print()
def trend(n=200,band=0.01):
    s=c.rolling(n).mean(); pos=pd.Series(np.nan,index=c.index); pos[c>s*(1+band)]=1; pos[c<s*(1-band)]=0; return pos.ffill().fillna(0)
def mr(entry, exitma=5, cond=None, n=2):
    r=rsi(c,n).values; mv=c.rolling(exitma).mean().values; cv=c.values; cd=cond.values
    pos=np.zeros(len(c)); inpos=False
    for i in range(len(c)):
        if not inpos and cd[i] and r[i]<entry: inpos=True
        elif inpos and cv[i]>mv[i]: inpos=False
        pos[i]=inpos
    return pd.Series(pos,index=c.index)
T=trend()
up=c>c.rolling(200).mean()
ev('BuyHold',pd.Series(1.0,index=c.index))
ev('Trend200 band1%',T)
ev('RSI2 uptrend (10/5)',mr(10,5,up))
ev('RSI2 downtrend only (5/5)',mr(5,5,~up))
combo=np.maximum(T, mr(5,5,T==0))
ev('Trend + downtrend RSI2 dips',combo)
ev('50/50 Trend & RSI2 sleeves',0.5*T+0.5*mr(10,5,up))
