"""Approximate backtest of SPY put credit spreads priced with Black-Scholes at VIX-based IV.
No historical option quotes are available here, so this is a model, not market data."""
import numpy as np, pandas as pd, sys
from math import log, sqrt, exp, erf
from lib import load_spx, D
def N(x): return 0.5*(1+erf(x/sqrt(2)))
def put(S,K,T,r,v):
    if T<=1e-6: return max(K-S,0.0)
    d1=(log(S/K)+(r+0.5*v*v)*T)/(v*sqrt(T)); d2=d1-v*sqrt(T)
    return K*exp(-r*T)*N(-d2)-S*N(-d1)
def strike_for_delta(S,T,r,v,delta):  # put delta negative; target |delta|
    from statistics import NormalDist
    d1=NormalDist().inv_cdf(1-delta)  # N(d1)-1 = -delta -> N(d1)=1-delta
    return S*exp(-(d1*v*sqrt(T)-(r+0.5*v*v)*T))
d=load_spx(); vix=pd.read_csv(D+'vix/data/vix-daily.csv',parse_dates=['DATE'],index_col='DATE')['CLOSE']
df=pd.DataFrame({'S':d.close/ d.close.loc['2025-12-19']*680,'rf':d.rf}).join(vix.rename('vix'),how='inner').dropna()
df['sma']=df.S.rolling(200).mean(); df['rate']=((1+df.rf)**252-1).clip(lower=0)
def sim(delta=0.20, width_pct=0.01, dte=45, tp=0.5, exit_dte=21, skew_s=1.10, skew_l=1.20, cost=0.10, trend=True, risk=0.5, start='1991', end='2025'):
    x=df[start:end]; dates=x.index; eq=1.0; curve=[]; trades=[]; pos=None
    S=x.S.values; V=x.vix.values/100; R=x.rate.values; SMA=x.sma.values
    for i in range(len(x)):
        if pos:
            T=(pos['exp']-dates[i]).days/365
            val=put(S[i],pos['Ks'],T,R[i],V[i]*skew_s)-put(S[i],pos['Kl'],T,R[i],V[i]*skew_l)
            pnl_frac=(pos['credit']-val)/pos['credit']
            if T<=exit_dte/365 or pnl_frac>=tp:
                close=val+cost/100  # pay half-spread+fees on exit (per share units)
                per=(pos['credit']-close)  # per share P&L
                ret=per/pos['maxloss']*risk  # fraction of sleeve
                eq*=1+ret; trades.append(per/pos['maxloss']); pos=None
        if pos is None and (not trend or S[i]>SMA[i]) and i<len(x)-1:
            T=dte/365; v=V[i]*skew_s
            Ks=strike_for_delta(S[i],T,R[i],v,delta); Ks=round(Ks); Kl=Ks-max(1,round(S[i]*width_pct))
            cr=put(S[i],Ks,T,R[i],v)-put(S[i],Kl,T,R[i],V[i]*skew_l)-cost/100
            if cr>0.05:
                pos={'Ks':Ks,'Kl':Kl,'credit':cr,'maxloss':(Ks-Kl)-cr,'exp':dates[i]+pd.Timedelta(days=dte)}
        curve.append(eq)
    c=pd.Series(curve,index=dates); r=c.pct_change().dropna()
    yrs=len(r)/252; cagr=c.iloc[-1]**(1/yrs)-1; dd=(c/c.cummax()-1).min()
    t=np.array(trades)
    return dict(CAGR=cagr,MaxDD=dd,Sharpe=r.mean()/r.std()*np.sqrt(252),Trades=len(t)/yrs,Win=(t>0).mean(),AvgWin=t[t>0].mean(),AvgLoss=t[t<=0].mean() if (t<=0).any() else 0), c
if __name__=='__main__':
    for per in [('1991','2007'),('2008','2025')]:
        print('=== period',per)
        for delta in [0.10,0.20,0.30]:
            for w in [0.0075,0.015]:
                for trend in [True,False]:
                    s,_=sim(delta,w,trend=trend,start=per[0],end=per[1])
                    print(f"delta {delta:.2f} width {w:.2%} trend {trend!s:5}: CAGR {s['CAGR']:6.1%} MaxDD {s['MaxDD']:6.1%} Sharpe {s['Sharpe']:.2f} trades/yr {s['Trades']:.1f} win {s['Win']:.0%} avgW {s['AvgWin']:.2f} avgL {s['AvgLoss']:.2f}")
