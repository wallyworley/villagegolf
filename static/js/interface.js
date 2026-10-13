(function initializeInterface(){
  'use strict';

  const accountOverlay=document.getElementById('account-overlay');
  const accountDialog=accountOverlay?.querySelector('.account-dialog');
  let accountReturnFocus=null;

  function focusableElements(container){
    if(!container) return [];
    return [...container.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),summary,[tabindex]:not([tabindex="-1"])')]
      .filter(element=>element.getClientRects().length>0);
  }

  window.openAccountDialog=function openAccountDialog(){
    if(!accountOverlay||!accountDialog) return;
    accountReturnFocus=document.activeElement;
    accountOverlay.classList.add('show');
    accountOverlay.setAttribute('aria-hidden','false');
    document.body.classList.add('modal-open');
    requestAnimationFrame(()=>accountDialog.focus());
  };

  window.closeAccountDialog=function closeAccountDialog(){
    if(!accountOverlay) return;
    const wasOpen=accountOverlay.classList.contains('show');
    accountOverlay.classList.remove('show');
    accountOverlay.setAttribute('aria-hidden','true');
    document.body.classList.remove('modal-open');
    if(wasOpen){
      const target=accountReturnFocus?.isConnected ? accountReturnFocus : document.getElementById('hdr-badge');
      requestAnimationFrame(()=>target?.focus());
    }
  };

  function syncNavigation(){
    document.querySelectorAll('.app-tab').forEach(tab=>{
      if(tab.classList.contains('active')) tab.setAttribute('aria-current','page');
      else tab.removeAttribute('aria-current');
    });
  }

  document.querySelector('.app-nav')?.addEventListener('click',()=>queueMicrotask(syncNavigation));
  syncNavigation();

  document.addEventListener('click',event=>{
    const toggle=event.target.closest('[data-password-toggle]');
    if(!toggle) return;
    const controls=String(toggle.dataset.passwordToggle||'').split(/\s+/).filter(Boolean);
    const reveal=toggle.getAttribute('aria-pressed')!=='true';
    controls.forEach(id=>{
      const input=document.getElementById(id);
      if(input) input.type=reveal?'text':'password';
    });
    toggle.setAttribute('aria-pressed',String(reveal));
    toggle.textContent=reveal?'Hide passwords':'Show passwords';
  });

  document.addEventListener('keydown',event=>{
    if(!accountOverlay?.classList.contains('show')) return;
    if(event.key==='Escape'){
      event.preventDefault();
      event.stopImmediatePropagation();
      window.closeAccountDialog();
      return;
    }
    if(event.key!=='Tab') return;
    const focusable=focusableElements(accountDialog);
    if(!focusable.length){ event.preventDefault(); accountDialog.focus(); return; }
    const first=focusable[0];
    const last=focusable[focusable.length-1];
    if(event.shiftKey&&document.activeElement===first){ event.preventDefault(); last.focus(); }
    else if(!event.shiftKey&&document.activeElement===last){ event.preventDefault(); first.focus(); }
  },true);

  function syncProgressiveSection(section){
    if(!section?.classList.contains('sec')) return;
    const locked=section.classList.contains('dim');
    section.setAttribute('aria-disabled',String(locked));
    section.querySelectorAll('button,input,select').forEach(control=>{
      if(locked){
        if(!control.disabled){
          control.dataset.progressiveDisabled='true';
          control.disabled=true;
        }
      }else if(control.dataset.progressiveDisabled==='true'){
        control.disabled=false;
        delete control.dataset.progressiveDisabled;
      }
    });
  }

  const app=document.getElementById('app');
  app?.querySelectorAll('.sec').forEach(syncProgressiveSection);
  if(app){
    new MutationObserver(mutations=>{
      const sections=new Set();
      mutations.forEach(mutation=>{
        if(mutation.type==='attributes') sections.add(mutation.target);
        else mutation.addedNodes.forEach(node=>{
          if(node.nodeType!==Node.ELEMENT_NODE) return;
          const section=node.matches?.('.sec')?node:node.closest?.('.sec');
          if(section) sections.add(section);
        });
      });
      sections.forEach(syncProgressiveSection);
    }).observe(app,{subtree:true,childList:true,attributes:true,attributeFilter:['class']});
  }
})();
