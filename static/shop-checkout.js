/* We Elect checkout: our own shipping form, then PayPal's buttons (PayPal + "Debit or Credit Card") on this page.
   The server prices everything; this script only sends the cart (product, color, size, quantity) and the address. */
(function(){
  var KEY='we_cart_v1', root=document.getElementById('checkout'), status=document.getElementById('checkout-status');
  var form=document.getElementById('ship-form'), pay=document.getElementById('pay'), slot=document.getElementById('paypal-buttons');
  var csrf=(document.querySelector('meta[name="csrf-token"]')||{}).content||'';
  function say(msg,ok){status.textContent=msg;status.hidden=!msg;status.className=ok?'note ok':'note';}
  var cart=[];try{cart=JSON.parse(localStorage.getItem(KEY)||'[]');}catch(e){}
  if(!Array.isArray(cart)||!cart.length){say('Your cart is empty. Head back to the shop to pick something out.');return;}
  root.hidden=false;
  function post(url,body){
    return fetch(url,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body||{})})
      .then(function(r){return r.json().catch(function(){return {};}).then(function(d){d._status=r.status;if(!r.ok&&!d.restart)throw new Error(d.error||'Something went wrong. Please try again.');return d;});});
  }
  function address(){var a={};['name','line1','line2','city','state','zip'].forEach(function(k){a[k]=(form.elements[k].value||'').trim();});return a;}
  function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
  function show(q){
    var box=document.getElementById('sum-lines');box.innerHTML='';
    q.lines.forEach(function(l){var row=el('div','cart-line');row.appendChild(el('span','cart-name',l.name+(l.q>1?' \u00d7 '+l.q:'')));row.appendChild(el('span','cart-amt',l.amount));box.appendChild(row);});
    document.getElementById('sum-items').textContent=q.items;document.getElementById('sum-ship-l').textContent=q.shipping_label;
    document.getElementById('sum-ship').textContent=q.shipping;document.getElementById('sum-tax').textContent=q.tax;
    document.getElementById('sum-total').textContent=q.total;document.getElementById('sum-totals').hidden=false;
    document.getElementById('sum-note').hidden=true;
  }
  var quoted=null, sdk=null, rendered=false;
  function loadSdk(){
    if(sdk)return sdk;
    sdk=new Promise(function(res,rej){
      var s=document.createElement('script');
      s.src='https://www.paypal.com/sdk/js?client-id='+encodeURIComponent(root.getAttribute('data-client-id'))+
            '&currency=USD&intent=capture&commit=true&components=buttons&disable-funding=venmo,paylater';
      s.setAttribute('data-page-type','checkout');s.async=true;
      s.onload=function(){window.paypal?res(window.paypal):rej();};s.onerror=function(){sdk=null;rej();};
      document.head.appendChild(s);
    });
    return sdk;
  }
  function render(paypal){
    if(rendered)return;rendered=true;
    var opts=function(source){return {
      fundingSource:source,
      style:source===paypal.FUNDING.PAYPAL?{layout:'vertical',shape:'rect',label:'pay',height:45,color:'gold'}:{layout:'vertical',shape:'rect',height:45,color:'black'},
      createOrder:function(){say('');return post('/shop/paypal/order',{items:cart,address:quoted}).then(function(d){return d.id;});},
      onApprove:function(data,actions){
        say('Confirming your payment\u2026',true);
        return post('/shop/paypal/order/'+encodeURIComponent(data.orderID)+'/capture',{}).then(function(d){
          if(d.restart){say(d.error);return actions.restart();}
          try{localStorage.removeItem(KEY);}catch(e){}
          window.location.assign(d.redirect);
        }).catch(function(e){say(e.message);});
      },
      onCancel:function(){say('Payment cancelled. You have not been charged, and your cart is still here.');},
      onError:function(e){say((e&&e.message&&e.message.indexOf('{')<0&&e.message.length<200)?e.message:'Payment could not be completed. You have not been charged. Please try again.');}
    };};
    [paypal.FUNDING.PAYPAL,paypal.FUNDING.CARD].forEach(function(src){
      var b=paypal.Buttons(opts(src));if(b.isEligible()){var d=el('div','pp-btn');slot.appendChild(d);b.render(d);}
    });
  }
  form.addEventListener('submit',function(ev){
    ev.preventDefault();say('');
    var a=address(), go=document.getElementById('ship-go');go.disabled=true;
    post('/shop/quote',{items:cart,address:a}).then(function(q){
      quoted=a;show(q);pay.hidden=false;
      return loadSdk().then(render,function(){say('We could not load PayPal. Please check your connection and try again.');});
    }).then(function(){pay.scrollIntoView({block:'nearest',behavior:'smooth'});})
      .catch(function(e){say(e.message);}).then(function(){go.disabled=false;});
  });
  /* Editing the address after a quote means shipping or tax may change: hide the buttons until it is re-quoted. */
  form.addEventListener('input',function(){if(quoted){quoted=null;pay.hidden=true;document.getElementById('sum-note').hidden=false;
    document.getElementById('sum-note').textContent='Address changed. Press \u201cContinue to payment\u201d to update shipping and tax.';}});
})();
