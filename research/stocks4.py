from lib import *
d=load_spx(); ret=d.tr.pct_change().fillna(0); c=d.close; rf=d.rf; C=0.0005
def trend(n=200,band=0.01):
    s=c.rolling(n).mean(); pos=pd.Series(np.nan,index=c.index); pos[c>s*(1+band)]=1; pos[c<s*(1-band)]=0; return pos.ffill().fillna(0)
grid=[(n,b) for n in [50,100,150,200,250,300] for b in [0,0.01,0.02]]
R={g:run(trend(*g),ret,C,rf)[0] for g in grid}
# walk-forward: train 20y, test next 5y, rolling from 1950
wf=[]; fixed=[]; picks=[]
for y in range(1950,2026,5):
    tr=(str(y-20),str(y-1)); te=(str(y),str(min(y+4,2025)))
    best=max(grid,key=lambda g: stats(R[g][tr[0]:tr[1]])['Sharpe'])
    picks.append((y,best)); wf.append(R[best][te[0]:te[1]]); fixed.append(R[(200,0.01)][te[0]:te[1]])
wf=pd.concat(wf); fixed=pd.concat(fixed)
print('picks',picks)
print('Walk-forward tuned 1950-2025:',fmt(stats(wf)))
print('Fixed 200/1% 1950-2025:     ',fmt(stats(fixed)))
print('BuyHold 1950-2025:          ',fmt(stats(ret['1950':]))) 
# lag2 robustness for blend
def mr(entry=10, exitma=5, n=2):
    up=(c>c.rolling(200).mean()).values
    r=rsi(c,n).values; mv=c.rolling(exitma).mean().values; cv=c.values
    pos=np.zeros(len(c)); inpos=False
    for i in range(len(c)):
        if not inpos and up[i] and r[i]<entry: inpos=True
        elif inpos and cv[i]>mv[i]: inpos=False
        pos[i]=inpos
    return pd.Series(pos,index=c.index)
blend=0.5*trend()+0.5*mr()
for lag in [1,2]:
    for cost in [0.0005,0.002]:
        r,t=run(blend,ret,cost,rf,lag); print(f'blend lag{lag} cost{cost}: OOS',fmt(stats(r['2000':],t)))
# yearly table OOS
r,t=run(blend,ret,C,rf); r2,_=run(trend(),ret,C,rf)
yr=pd.DataFrame({'BuyHold':ret,'Trend':r2,'Blend':r})['2000':].groupby(lambda x:x.year).apply(lambda x:(1+x).prod()-1)
print((yr*100).round(1).to_string())
yr.to_csv('stock_yearly.csv')
eq=pd.DataFrame({'BuyHold':(1+ret['2000':]).cumprod(),'Trend':(1+r2['2000':]).cumprod(),'Blend':(1+r['2000':]).cumprod()})
eq.resample('W').last().to_csv('stock_equity.csv')
