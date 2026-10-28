/* Cart for the We Elect shop. The cart lives in this browser (localStorage); prices shown here are for display only,
   the server re-prices every line from data/shop_products.json when you check out. */
(function(){
  var KEY='we_cart_v1', data=JSON.parse(document.getElementById('shop-data').textContent), P={};
  data.products.forEach(function(p){P[p.id]=p;});
  var maxQ=(data.limits&&data.limits.max_qty_per_line)||10, maxLines=(data.limits&&data.limits.max_lines)||8;
  function load(){try{var c=JSON.parse(localStorage.getItem(KEY)||'[]');return Array.isArray(c)?c.filter(valid):[];}catch(e){return [];}}
  function save(c){try{localStorage.setItem(KEY,JSON.stringify(c));}catch(e){}}
  function valid(l){var p=l&&P[l.p];return !!(p&&p.ok[l.c+':'+l.s]&&l.q>=1&&l.q<=maxQ);}
  function money(c){return '$'+(c/100).toFixed(2);}
  function colorName(p,id){for(var i=0;i<p.colors.length;i++)if(p.colors[i].id===id)return p.colors[i].name;return id;}
  var linesEl=document.getElementById('cart-lines'), totalEl=document.getElementById('cart-total'), shipEl=document.getElementById('cart-ship'),
      goEl=document.getElementById('cart-checkout'), clearEl=document.getElementById('cart-clear'), cart=load();
  function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
  function render(){
    linesEl.innerHTML='';
    if(!cart.length){linesEl.appendChild(el('p','fine','Your cart is empty.'));totalEl.hidden=shipEl.hidden=goEl.hidden=clearEl.hidden=true;return;}
    var sub=0,n=0;
    cart.forEach(function(l,i){
      var p=P[l.p], row=el('div','cart-line');
      var name=p.name+' ('+colorName(p,l.c)+(l.s==='One size'?'':', '+l.s)+')';
      row.appendChild(el('span','cart-name',name));
      var q=el('span','cart-q');
      var minus=el('button','linkbtn','\u2212');minus.type='button';minus.setAttribute('aria-label','One fewer '+name);
      var plus=el('button','linkbtn','+');plus.type='button';plus.setAttribute('aria-label','One more '+name);
      minus.onclick=function(){l.q-=1;if(l.q<1)cart.splice(i,1);save(cart);render();};
      plus.onclick=function(){if(l.q<maxQ){l.q+=1;save(cart);render();}};
      q.appendChild(minus);q.appendChild(el('span','cart-qn',String(l.q)));q.appendChild(plus);row.appendChild(q);
      row.appendChild(el('span','cart-amt',money(p.price_cents*l.q)));
      linesEl.appendChild(row);sub+=p.price_cents*l.q;n+=l.q;
    });
    var ship=data.shipping.first+data.shipping.more*(n-1);
    totalEl.textContent='Items: '+money(sub);totalEl.hidden=false;
    shipEl.textContent=data.shipping.label+': '+money(ship)+'. Sales tax, if any, is shown at checkout.';shipEl.hidden=false;
    goEl.hidden=false;clearEl.hidden=false;
  }
  function add(pid){
    var root=document.querySelector('.prod[data-pid="'+pid+'"]');
    var color=root.querySelector('input[name="color-'+pid+'"]:checked').value, sizeEl=root.querySelector('[name="size-'+pid+'"]');
    var q=parseInt(root.querySelector('[name="qty-'+pid+'"]').value,10);
    if(!(q>=1)||q>maxQ){q=1;}
    var size=sizeEl.value, found=null;
    cart.forEach(function(l){if(l.p===pid&&l.c===color&&l.s===size)found=l;});
    if(found){found.q=Math.min(maxQ,found.q+q);}
    else{if(cart.length>=maxLines)return;cart.push({p:pid,c:color,s:size,q:q});}
    save(cart);render();
    document.getElementById('cart').scrollIntoView({block:'nearest',behavior:'smooth'});
  }
  Array.prototype.forEach.call(document.querySelectorAll('.add-btn'),function(b){b.addEventListener('click',function(){add(b.getAttribute('data-pid'));});});
  Array.prototype.forEach.call(document.querySelectorAll('.prod input[type=radio]'),function(r){r.addEventListener('change',function(){
    var img=r.closest('.prod').querySelector('.prod-img');if(img&&r.getAttribute('data-img'))img.src=r.getAttribute('data-img');});});
  clearEl.addEventListener('click',function(){cart=[];save(cart);render();});
  render();
})();
