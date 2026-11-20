from lib import *
d=load_btc(); c=d.close; ret=c.pct_change().fillna(0); PPY=365
def thresh(w,th):
    out=np.zeros(len(w)); cur=0.0
    for i,x in enumerate(w.fillna(0).values):
        if abs(x-cur)>=th or (x==0 and cur!=0): cur=x
        out[i]=cur
    return pd.Series(out,index=w.index)
sig=pd.concat([(c>c.rolling(n).mean()).astype(float) for n in [50,100,150]]+[(c.pct_change(L)>0).astype(float) for L in [28,56,120]],axis=1).mean(axis=1)
rv=ret.rolling(30).std()*np.sqrt(PPY)
w=thresh((sig*(0.4/rv).clip(upper=1)).fillna(0),0.10)
r,t=run(w,ret,0.003)
print('Avg weight OOS',w['2021':].mean(), 'avg one-way turnover/yr', t['2021':].sum()/5.8)
yr=pd.DataFrame({'BTC hold':ret,'Bot':r})['2014':].groupby(lambda x:x.year).apply(lambda x:(1+x).prod()-1)
print((yr*100).round(1).to_string()); yr.to_csv('crypto_yearly.csv')
eq=pd.DataFrame({'BTC hold':(1+ret['2021':]).cumprod(),'Bot':(1+r['2021':]).cumprod()}); eq.resample('W').last().to_csv('crypto_equity.csv')
# hourly mean reversion day-trading test
h=pd.read_csv('/tmp/claude-0/-home-claude/93eeb467-17cc-53c0-951f-80ec2394ca98/scratchpad/data/btc_1h.csv.gz',parse_dates=['ts'],index_col='ts'); h=h['2018':]
hc=h.close; hr=hc.pct_change().fillna(0); R=rsi(hc,14)
pos=pd.Series(np.nan,index=hc.index); pos[R<30]=1; pos[R>50]=0; pos=pos.ffill().fillna(0)
for cost in [0,0.001,0.003]:
    rr,tt=run(pos,hr,cost); print(f'Hourly RSI14 dip-buy cost {cost:.1%}:',fmt(stats(rr,tt,24*365)))
