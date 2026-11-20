from options1 import sim
cfgs=[dict(delta=0.2,width_pct=0.015),dict(delta=0.3,width_pct=0.015),dict(delta=0.2,width_pct=0.03),dict(delta=0.3,width_pct=0.03)]
for c in cfgs:
  for risk in [0.2]:
    for skew in [(1.10,1.20),(1.05,1.10),(1.15,1.30)]:
      for cost in [0.05,0.15]:
        out=[]
        for per in [('1991','2007'),('2008','2025')]:
            s,_=sim(**c,risk=risk,skew_s=skew[0],skew_l=skew[1],cost=cost,start=per[0],end=per[1])
            out.append(f"{s['CAGR']:6.1%}/{s['MaxDD']:6.1%}/{s['Sharpe']:.2f}")
        print(c,'risk',risk,'skew',skew,'cost',cost,'| IS',out[0],'| OOS',out[1])
