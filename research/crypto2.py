from lib import *
d=load_btc(); c=d.close; ret=c.pct_change().fillna(0); PPY=365
P={'IS 13-20':('2013','2020'),'OOS 21-26':('2021','2026')}
def thresh(w,th):
    out=np.zeros(len(w)); cur=0.0
    for i,x in enumerate(w.fillna(0).values):
        if abs(x-cur)>=th or (x==0 and cur!=0): cur=x
        out[i]=cur
    return pd.Series(out,index=w.index)
def p(name,pos,cost=0.003,lag=1):
    r,t=run(pos,ret,cost,None,lag); w=pos.shift(lag).fillna(0)
    for k,(a,b) in P.items(): print(f"{name:34s} {k} {fmt(stats(r[a:b],t,PPY,w))}")
    return r
sig=pd.concat([(c>c.rolling(n).mean()).astype(float) for n in [50,100,150]]+[(c.pct_change(L)>0).astype(float) for L in [28,56,120]],axis=1).mean(axis=1)
p('Ensemble trend (6 signals)',sig)
rv=ret.rolling(30).std()*np.sqrt(PPY)
for tv in [0.3,0.4,0.5,0.6]:
    w=(sig*(tv/rv).clip(upper=1)).fillna(0)
    p(f'Ensemble + volTarget {tv:.0%}',w)
    p(f'  ...+10% rebalance band',thresh(w,0.10))
w=thresh((sig*(0.4/rv).clip(upper=1)).fillna(0),0.10)
p('Chosen @0.5% cost (Kraken-ish)',w,cost=0.005)
p('Chosen lag2',w,lag=2)
bh=pd.Series(1.0,index=c.index)
p('BuyHold',bh)
p('BuyHold volTarget 40%',thresh((0.4/rv).clip(upper=1).fillna(0),0.10))
