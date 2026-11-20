from lib import *
d=load_spx(); ret=d.tr.pct_change().fillna(0); c=d.close; rf=d.rf
C=0.0005
periods={'IS 1928-1999':('1928','1999'),'OOS 2000-2025':('2000','2025'),'Post-pub 2009-2025':('2009','2025')}
def show(name,pos,lag=1):
    r,t=run(pos,ret,C,rf,lag); w=pos.shift(lag).fillna(0)
    for p,(a,b) in periods.items(): print(f"{name:28s} {p:20s}", fmt(stats(r[a:b],t,w=w)))
show('BuyHold',pd.Series(1.0,index=c.index))
for n in [200]:
    show(f'SMA{n}',(c>c.rolling(n).mean()).astype(float))
    show(f'SMA{n} lag2',(c>c.rolling(n).mean()).astype(float),lag=2)
# Connors RSI2 original: close>SMA200, RSI2<10 entry, exit close>SMA5
def rsi2(entry=10, exitma=5, trend=200, n=2):
    r=rsi(c,n); sma=c.rolling(trend).mean(); ma5=c.rolling(exitma).mean()
    pos=np.zeros(len(c)); inpos=False
    rv=r.values; cv=c.values; sv=sma.values; mv=ma5.values
    for i in range(len(c)):
        if not inpos and cv[i]>sv[i] and rv[i]<entry: inpos=True
        elif inpos and cv[i]>mv[i]: inpos=False
        pos[i]=1.0 if inpos else 0.0
    return pd.Series(pos,index=c.index)
show('RSI2 Connors',rsi2())
show('RSI2 Connors lag2',rsi2(),lag=2)
