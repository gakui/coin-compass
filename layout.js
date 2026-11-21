/* Coin Compass layout: five tabs, no "More" menu.
   Loaded by one line at the end of index.html. */
(function(){
  const st=document.createElement('style');
  st.textContent='.tabs{grid-template-columns:repeat(5,1fr)}.segs{margin-bottom:4px}';
  document.head.appendChild(st);

  const ICON={
    market:'<path d="M3 17l5-6 4 4 8-9"/><path d="M15 6h5v5"/>',
    outlook:'<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
    news:'<path d="M4 5h13v14H6a2 2 0 0 1-2-2z"/><path d="M17 9h3v8a2 2 0 0 1-2 2"/><path d="M8 9h5M8 13h5"/>',
    lab:'<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3"/><path d="M7.5 15h9"/>',
    alerts:'<path d="M6 17V11a6 6 0 0 1 12 0v6l1.5 2h-15z"/><path d="M10 21h4"/>'
  };
  const TABS=[
    ['market','Market',[['coins','Coins'],['holdings','Holdings'],['macro','Macro']]],
    ['outlook','Outlook',[['signals','Signals'],['forecast','Forecast'],['fund','Fundamentals']]],
    ['news','News',null],
    ['lab','Lab',[['paper','Paper bot'],['bt','Backtest']]],
    ['alerts','Alerts',[['alerts','My alerts'],['settings','Settings']]]
  ];
  const VIEW={coins:()=>marketView(),holdings:()=>pfView(),macro:()=>macroView(),signals:()=>signalsView(),forecast:()=>fcView(),fund:()=>fundView(),paper:()=>paperView(),bt:()=>btView(),alerts:()=>alertsView(),settings:()=>settingsView()};
  const OLD={signals:['outlook','signals'],forecast:['outlook','forecast'],more:['market','coins']};
  S.seg={market:'coins',outlook:'signals',lab:'paper',alerts:'alerts'};
  const strip=h=>h.replace(/^<button class="btn ghost" data-act="sub" data-s=""[^>]*>[^<]*<\/button><h2[^>]*>[^<]*<\/h2>/,'');

  render=function(){
    if(OLD[S.tab]){S.seg[OLD[S.tab][0]]=OLD[S.tab][1];S.tab=OLD[S.tab][0]}
    const y=scrollY,T=TABS.find(t=>t[0]===S.tab)||TABS[0],segs=T[2];
    let h='';
    if(segs){
      const cur=S.seg[T[0]];
      h+=`<div class="chips segs">${segs.map(s=>`<button data-act="seg" data-s="${s[0]}" class="${cur===s[0]?'on':''}">${s[1]}</button>`).join('')}</div>`+strip(VIEW[cur]());
    }else h=newsView();
    const keep={};
    document.querySelectorAll('main input,main select,main textarea').forEach(e=>{if(e.id)keep[e.id]=e.value});
    $('#view').innerHTML=h;
    Object.entries(keep).forEach(([k,v])=>{const e=document.getElementById(k);if(e)e.value=v});
    scrollTo(0,y);
    $('#tabs').innerHTML=TABS.map(t=>`<button data-act="tab" data-t="${t[0]}" class="${S.tab===t[0]?'on':''}" aria-label="${t[1]}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${ICON[t[0]]}</svg>${t[1]}</button>`).join('');
    $('#stamp').textContent=S.updated?'Updated '+S.updated.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'Loading…';
  };

  /* Tapping Market always lands on the coin list; other tabs remember where you were. */
  document.addEventListener('click',e=>{
    const t=e.target.closest('[data-act="tab"]');
    if(t&&t.dataset.t==='market')S.seg.market='coins';
  },true);
  document.addEventListener('click',e=>{
    const el=e.target.closest('[data-act="seg"]');if(!el)return;
    S.seg[S.tab]=el.dataset.s;render();scrollTo(0,0);
  });
  render();
})();
