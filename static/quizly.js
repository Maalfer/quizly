/* Quizly — helpers compartidos: avatares con foto, sonidos, confeti, PWA. */
(function(){
  const Q = window.Q = {};

  // --- Avatar: emoji o foto subida ---
  // Solo se acepta como imagen una ruta propia /static/uploads/<hash>.<ext>
  // (el mismo formato que devuelve /upload/avatar). Cualquier otra cosa
  // (URL externa, data:, etc.) se trata como emoji para no cargar recursos
  // de terceros ni poder romper el atributo src.
  Q.isImg = a => typeof a==='string' && /^\/static\/uploads\/[0-9a-f]{20}\.(png|jpg|webp|gif)$/.test(a);
  Q.avatarHtml = (a, cls='') => Q.isImg(a)
    ? '<img class="av-img '+cls+'" src="'+Q.esc(a)+'" alt="">'
    : '<span class="av-emoji '+cls+'">'+Q.esc(a||'🦊')+'</span>';

  // --- Sonidos (WebAudio, sin ficheros) ---
  let ctx=null, muted=localStorage.getItem('quizly_muted')==='1';
  function ac(){ if(!ctx){try{ctx=new (window.AudioContext||window.webkitAudioContext)();}catch(e){}} return ctx; }
  function tone(freq,dur,type='sine',vol=0.18,when=0){
    if(muted)return; const a=ac(); if(!a)return;
    const o=a.createOscillator(),g=a.createGain();
    o.type=type; o.frequency.value=freq;
    const t=a.currentTime+when;
    g.gain.setValueAtTime(0,t); g.gain.linearRampToValueAtTime(vol,t+0.01);
    g.gain.exponentialRampToValueAtTime(0.0001,t+dur);
    o.connect(g); g.connect(a.destination); o.start(t); o.stop(t+dur);
  }
  Q.sfx = {
    tick(){ tone(880,0.05,'square',0.06); },
    select(){ tone(520,0.08,'triangle',0.12); },
    correct(){ tone(660,0.12,'sine',0.2); tone(880,0.16,'sine',0.2,0.1); tone(1180,0.2,'sine',0.2,0.22); },
    wrong(){ tone(200,0.3,'sawtooth',0.16); },
    join(){ tone(740,0.1,'triangle',0.12); tone(990,0.12,'triangle',0.12,0.08); },
    start(){ tone(440,0.12,'sine',0.18); tone(660,0.14,'sine',0.18,0.12); tone(880,0.18,'sine',0.18,0.26); },
    boost(){ tone(300,0.1,'square',0.14); tone(600,0.1,'square',0.14,0.08); tone(1200,0.14,'square',0.14,0.16); },
    podium(){ [523,659,784,1047].forEach((f,i)=>tone(f,0.25,'sine',0.2,i*0.14)); }
  };
  Q.muted = ()=>muted;
  Q.toggleMute = ()=>{ muted=!muted; localStorage.setItem('quizly_muted',muted?'1':'0'); return muted; };
  Q.resume = ()=>{ const a=ac(); if(a&&a.state==='suspended')a.resume(); };

  // --- Confeti (canvas, sin librería) ---
  Q.confetti = function(ms=2600){
    if(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    if(document.getElementById('cfcanvas'))return;
    const cv=document.createElement('canvas'); cv.id='cfcanvas';
    cv.style.cssText='position:fixed;inset:0;pointer-events:none;z-index:9999';
    document.body.appendChild(cv);
    const x=cv.getContext('2d'); let W,H;
    function size(){W=cv.width=innerWidth;H=cv.height=innerHeight;} size(); addEventListener('resize',size);
    const cols=['#f59e0b','#10b981','#3b82f6','#ef4444','#22d3ee','#ffd700','#8b5cf6'];
    const P=Array.from({length:160},()=>({x:Math.random()*W,y:-20-Math.random()*H,
      r:4+Math.random()*6,c:cols[Math.random()*cols.length|0],
      vy:2+Math.random()*4,vx:-2+Math.random()*4,rot:Math.random()*6,vr:-0.2+Math.random()*0.4}));
    const t0=Date.now();
    (function loop(){
      x.clearRect(0,0,W,H);
      P.forEach(p=>{p.x+=p.vx;p.y+=p.vy;p.rot+=p.vr;if(p.y>H+20){p.y=-20;p.x=Math.random()*W;}
        x.save();x.translate(p.x,p.y);x.rotate(p.rot);x.fillStyle=p.c;x.fillRect(-p.r/2,-p.r/2,p.r,p.r*0.6);x.restore();});
      if(Date.now()-t0<ms)requestAnimationFrame(loop);
      else{cv.remove();}
    })();
  };

  // --- Tema claro/oscuro ---
  Q.getTheme = ()=>{ try{return localStorage.getItem('quizly_theme')||'dark';}catch(e){return 'dark';} };
  Q.applyTheme = t => document.documentElement.classList.toggle('light', t==='light');
  Q.toggleTheme = ()=>{
    const t = Q.getTheme()==='light' ? 'dark' : 'light';
    try{ localStorage.setItem('quizly_theme', t); }catch(e){}
    Q.applyTheme(t);
    const b=document.getElementById('themeToggle'); if(b) b.textContent = t==='light'?'🌙':'☀️';
  };
  window.addEventListener('DOMContentLoaded',()=>{
    Q.applyTheme(Q.getTheme());
    if(!document.getElementById('themeToggle')){
      const b=document.createElement('button');
      b.id='themeToggle'; b.className='theme-toggle'; b.type='button';
      b.title='Cambiar tema claro/oscuro'; b.setAttribute('aria-label','Cambiar tema');
      b.textContent = Q.getTheme()==='light' ? '🌙' : '☀️';
      b.onclick=Q.toggleTheme; document.body.appendChild(b);
    }
  });

  // --- PWA --- (v2: el SW ya no cachea HTML; query para saltar la caché de Cloudflare)
  if('serviceWorker' in navigator){ window.addEventListener('load',()=>navigator.serviceWorker.register('/sw.js?v=2').catch(()=>{})); }

  // textContent ya neutraliza &<> para contenido de texto, pero NO las
  // comillas -- y Q.esc() se usa también dentro de atributos value="..."/
  // src="...", donde una comilla sin escapar rompe el atributo igualmente.
  // Se escapan además aquí para que sea segura en ambos contextos.
  Q.esc = s => { const d=document.createElement('div'); d.textContent=s==null?'':s;
    return d.innerHTML.replace(/"/g,'&quot;').replace(/'/g,'&#39;'); };
})();
