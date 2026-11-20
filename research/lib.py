import numpy as np, pandas as pd
import os
D=os.getenv('DATA_DIR','data/')  # folder holding spx/ and btc_1d.csv, see research/README.md
def load_spx():
    d=pd.read_csv(D+'spx/data/us_market_data.csv',parse_dates=['Date']).set_index('Date')
    d=d[d.index>='1928-01-01']
    out=pd.DataFrame({'close':d['Close'],'tr':d['Adjusted Close']})
    # daily risk-free return series
    out['rf']=d['Risk Free Return'].pct_change().fillna(0)
    return out
def load_btc():
    d=pd.read_csv(D+'btc_1d.csv',parse_dates=['ts'],index_col='ts'); d.index=d.index.tz_localize(None)
    return d[d.index>='2013-01-01']
def rsi(c,n):
    d=c.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    # Wilder smoothing
    au=up.ewm(alpha=1/n,adjust=False).mean(); ad=dn.ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+au/ad.replace(0,np.nan))
def run(pos, ret, cost, rf=None, lag=1):
    """pos: target weight decided at bar t close; applied to return of bar t+lag.
    ret: asset simple returns. cost: one-way cost fraction. rf: cash return."""
    w=pos.shift(lag).fillna(0)
    turn=w.diff().abs().fillna(w.abs())
    r=w*ret - turn*cost
    if rf is not None: r=r+(1-w).clip(lower=0)*rf
    return r, turn
def stats(r, turn=None, ppy=252, w=None):
    r=r.dropna()
    if len(r)<2: return {}
    eq=(1+r).cumprod(); yrs=len(r)/ppy
    cagr=eq.iloc[-1]**(1/yrs)-1; vol=r.std()*np.sqrt(ppy)
    sh=r.mean()/r.std()*np.sqrt(ppy) if r.std()>0 else 0
    dd=(eq/eq.cummax()-1).min()
    out={'CAGR':cagr,'Vol':vol,'Sharpe':sh,'MaxDD':dd,'MAR':cagr/abs(dd) if dd<0 else np.nan}
    if turn is not None: out['Trades/yr']=(turn.loc[r.index]>0).sum()/yrs
    if w is not None: out['Exposure']=(w.loc[r.index]>0).mean()
    return out
def fmt(s):
    return ' '.join(f"{k}={v:.2%}" if k in('CAGR','Vol','MaxDD','Exposure') else f"{k}={v:.2f}" for k,v in s.items())
