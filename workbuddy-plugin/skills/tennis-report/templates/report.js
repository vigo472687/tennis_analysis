/* Local report controls; deliberately no fetch, telemetry or external messaging. */
document.querySelector('[data-print]')?.addEventListener('click',()=>window.print());
for(const control of document.querySelectorAll('[data-speed]'))control.addEventListener('change',()=>{const video=control.closest('figure')?.querySelector('video');const speed=Number(control.value);if(video&&[0.25,0.5,1].includes(speed))video.playbackRate=speed;});
for(const media of document.querySelectorAll('[data-media]')){
  const failed=()=>{const container=media.closest('figure')||media.parentElement;let message=container.querySelector('.resource-error');if(!message){message=document.createElement('p');message.className='resource-error';message.textContent='本地示意资源不可用，请恢复资源后再查看。';container.append(message);}message.hidden=false;media.hidden=true;};
  media.addEventListener('error',failed);
  if(media.tagName==='IMG'&&media.complete&&media.naturalWidth===0)failed();
}
document.querySelector('[data-copy-feedback]')?.addEventListener('click',async event=>{
  const field=document.querySelector('[data-feedback]'),status=document.querySelector('[data-copy-status]');if(!field||!status)return;
  const button=event.currentTarget;button.disabled=true;
  try{if(!navigator.clipboard?.writeText)throw new Error('clipboard unavailable');await navigator.clipboard.writeText(field.value);status.textContent='已复制文字草稿；未附加材料或发送邮件。';}
  catch{field.focus();field.select();status.textContent='可使用系统复制命令复制已选中的草稿；未发送任何内容。';}
  finally{button.disabled=false;}
});
