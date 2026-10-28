/* Loads the Ko-fi form only when the visitor asks for it (no third-party request before that). */
(function(){
  var b=document.getElementById('kofi-show'), slot=document.getElementById('kofi-slot');
  if(!b||!slot) return;
  b.hidden=false;
  b.addEventListener('click',function(){
    if(slot.firstChild){slot.innerHTML='';b.setAttribute('aria-expanded','false');b.textContent='Show the Ko-fi form here';return;}
    var f=document.createElement('iframe');
    f.src=b.getAttribute('data-src'); f.title='Give to We Elect on Ko-fi'; f.className='kofi-frame'; f.height='712';
    f.setAttribute('allow','payment'); f.setAttribute('loading','lazy'); f.setAttribute('referrerpolicy','strict-origin-when-cross-origin');
    slot.appendChild(f); b.setAttribute('aria-expanded','true'); b.textContent='Hide the Ko-fi form';
  });
})();
