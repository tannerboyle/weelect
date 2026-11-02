/* One-time gifts on the Support page: pick or type an amount, then PayPal's buttons (PayPal + "Debit or Credit Card").
   The server validates the amount and creates the PayPal order; this script only sends the amount the reader chose. */
(function(){
  var form=document.getElementById('gift-form');if(!form)return;
  var input=document.getElementById('gift-amount'),status=document.getElementById('gift-status'),pay=document.getElementById('gift-pay'),
      slot=document.getElementById('gift-buttons'),sum=document.getElementById('gift-sum'),go=document.getElementById('gift-go');
  var picks=form.querySelectorAll('.gift-pick'),csrf=(document.querySelector('meta[name="csrf-token"]')||{}).content||'';
  var chosen=null,sdk=null,rendered=false;
  function say(m,ok){status.textContent=m;status.hidden=!m;status.className=ok?'note ok':'note';}
  function norm(v){v=(v||'').replace(/[$,\s]/g,'');if(!/^\d{1,6}(\.\d{1,2})?$/.test(v))return null;var n=Math.round(parseFloat(v)*100);return n;}
  function fmt(c){return '$'+(c/100).toLocaleString('en-US',{minimumFractionDigits:c%100?2:0,maximumFractionDigits:2});}
  function reset(){chosen=null;pay.hidden=true;}
  Array.prototype.forEach.call(picks,function(b){b.addEventListener('click',function(){
    Array.prototype.forEach.call(picks,function(x){x.setAttribute('aria-pressed',String(x===b));});
    input.value=b.getAttribute('data-amt');reset();say('');});});
  input.addEventListener('input',function(){Array.prototype.forEach.call(picks,function(x){x.setAttribute('aria-pressed',String(x.getAttribute('data-amt')===input.value.trim()));});reset();});
  function post(url,body){
    return fetch(url,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body||{})})
      .then(function(r){return r.json().catch(function(){return {};}).then(function(d){if(!r.ok&&!d.restart)throw new Error(d.error||'Something went wrong. Please try again.');return d;});});
  }
  function loadSdk(){
    if(sdk)return sdk;
    sdk=new Promise(function(res,rej){
      var s=document.createElement('script');
      s.src='https://www.paypal.com/sdk/js?client-id='+encodeURIComponent(form.getAttribute('data-client-id'))+
            '&currency=USD&intent=capture&commit=true&components=buttons&disable-funding=venmo,paylater';
      s.async=true;s.onload=function(){window.paypal?res(window.paypal):rej();};s.onerror=function(){sdk=null;rej();};
      document.head.appendChild(s);
    });
    return sdk;
  }
  function render(paypal){
    if(rendered)return;rendered=true;
    [paypal.FUNDING.PAYPAL,paypal.FUNDING.CARD].forEach(function(src){
      var b=paypal.Buttons({fundingSource:src,
        style:src===paypal.FUNDING.PAYPAL?{layout:'vertical',shape:'rect',label:'pay',height:45,color:'gold'}:{layout:'vertical',shape:'rect',height:45,color:'black'},
        createOrder:function(){say('');return post('/shop/paypal/donation',{amount:(chosen/100).toFixed(2)}).then(function(d){return d.id;});},
        onApprove:function(data,actions){
          say('Confirming your gift\u2026',true);
          return post('/shop/paypal/order/'+encodeURIComponent(data.orderID)+'/capture',{}).then(function(d){
            if(d.restart){say(d.error);return actions.restart();}
            window.location.assign(d.redirect);
          }).catch(function(e){say(e.message);});
        },
        onCancel:function(){say('Cancelled. You have not been charged.');},
        onError:function(){say('Your gift could not be completed. You have not been charged. Please try again.');}
      });
      if(b.isEligible()){var d=document.createElement('div');d.className='pp-btn';slot.appendChild(d);b.render(d);}
    });
  }
  form.addEventListener('submit',function(ev){
    ev.preventDefault();
    var c=norm(input.value);
    if(c===null){say('Please enter an amount in dollars, like 25 or 25.50.');return;}
    if(c<100){say('The smallest gift we can accept online is $1.');return;}
    if(c>1000000){say('Online gifts are limited to $10,000.');return;}
    chosen=c;say('');sum.textContent='Your one-time gift: '+fmt(c)+'. Choose how to pay:';pay.hidden=false;go.disabled=true;
    loadSdk().then(render,function(){say('We could not load PayPal. Please check your connection and try again.');}).then(function(){go.disabled=false;pay.scrollIntoView({block:'nearest',behavior:'smooth'});});
  });
})();
