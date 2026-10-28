/* Mounts Stripe embedded Checkout. Card details go straight from Stripe's iframe to Stripe. */
(function(){
  var KEY='we_cart_v1', box=document.getElementById('checkout'), status=document.getElementById('checkout-status');
  function say(msg){status.textContent=msg;status.hidden=false;}
  var cart=[];try{cart=JSON.parse(localStorage.getItem(KEY)||'[]');}catch(e){}
  if(!Array.isArray(cart)||!cart.length){say('Your cart is empty. Head back to the shop to pick something out.');return;}
  var csrf=(document.querySelector('meta[name="csrf-token"]')||{}).content||'';
  var s=document.createElement('script');s.src='https://js.stripe.com/v3/';s.async=true;
  s.onerror=function(){say('We could not load the secure payment form. Please check your connection and try again.');};
  s.onload=function(){
    var stripe=window.Stripe(box.getAttribute('data-pk'));
    var create=stripe.createEmbeddedCheckoutPage||stripe.initEmbeddedCheckout;
    function fetchClientSecret(){
      return fetch('/shop/checkout',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({items:cart})})
        .then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.error||'Checkout could not start.');return d.clientSecret;});});
    }
    create.call(stripe,{fetchClientSecret:fetchClientSecret}).then(function(c){c.mount('#checkout');})
      .catch(function(e){say((e&&e.message)||'Checkout could not start. Please try again in a few minutes.');});
  };
  document.head.appendChild(s);
})();
