import type {ActionSelection} from './action-selection.js';

export function actionCoverage(selection:ActionSelection|null,frame:number):'selected'|'excluded'|'unknown'|'outside' {
  if(!selection?.details_available)return 'unknown';
  if(frame<0||frame>=selection.window_frames)return 'outside';
  return selection.selected_segments!.some(s=>frame>=s.start_frame&&frame<=s.end_frame)?'selected':'excluded';
}
export const actionSelectionPanel=(prefix:string)=>`<section class="panel action-selection" aria-label="候选动作段覆盖"><div class="panel-head"><h2>候选动作段 · 最多 10 段</h2><a class="btn" id="${prefix}-selection-download" hidden download="action-selection.json">选择 JSON</a></div><div id="${prefix}-selection-content" class="action-content"><p>加载任务后显示该任务的分段覆盖。</p></div></section>`;
export const actionSelectionStyles=String.raw`
.action-selection{margin-top:20px}.action-content{padding:17px;min-width:0}.action-content p{font-size:12px;line-height:1.9;color:var(--muted)}.action-content .action-totals{display:flex;flex-wrap:wrap;gap:10px 24px;margin-bottom:12px;font-size:12px}.action-totals strong{font:18px ui-monospace,monospace;margin-right:5px;color:var(--ink)}.action-coverage{display:block;width:100%;height:28px;margin:12px 0}.action-segments{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.action-segments button{display:block;white-space:normal;text-align:left;line-height:1.7;min-height:44px}.action-segments small{display:block;font:10px ui-monospace,monospace;color:var(--muted)}.action-selection .panel-head{flex-wrap:wrap;gap:10px}.action-content .action-note{color:#865f29}
`;
export const actionSelectionScript=actionCoverage.toString()+String.raw`
function actionRender(prefix,selection,url,seek){
 const el=$(prefix+'-selection-content'),link=$(prefix+'-selection-download');link.hidden=!url;if(url)link.href=url;
 if(!selection){el.innerHTML='<p>此任务未记录 action-selection.json；不能判断选择了几段，也不能把无记录时间当成主动排除。旧任务不会自动补算。</p>';el.onclick=null;return;}
 const s=selection,segments=s.selected_segments||[];
 const totals=[['选中候选段',s.selected_count+' / '+s.max_actions],['发现候选段',s.segments_found],['检测窗口',s.window_frames+' 帧'],['选中',s.selected_frames+' 帧'],['主动排除',s.excluded_frames+' 帧']];
 el.innerHTML='<div class="action-totals">'+totals.map(([k,v])=>'<span><strong>'+esc(v)+'</strong>'+esc(k)+'</span>').join('')+'</div><p>按时间顺序选前 10 个候选动作窗。依据主体框运动启发式分段，未检测球/球拍，不能据此确认完整击球；少于 10 段如实显示。</p>'+(s.details_available?'<svg class="action-coverage" viewBox="0 0 1000 20" preserveAspectRatio="none" role="img" aria-label="候选动作覆盖：绿色选中，灰色主动排除"><rect x="0" y="0" width="1000" height="20" rx="4" fill="#e6eae7"/>'+segments.map(x=>'<rect x="'+(x.start_frame/Math.max(1,s.window_frames)*1000)+'" y="0" width="'+(x.frames/Math.max(1,s.window_frames)*1000)+'" height="20" fill="#287c56"/>').join('')+'</svg><p>绿色：姿态选中推理 · 灰色：姿态主动排除；找人跟踪仍保留全窗口记录。时间与帧号沿用标准化视频，未重编号。</p><div class="action-segments">'+segments.map((x,i)=>'<button class="btn" '+(seek?'':'disabled ')+'data-action-segment="'+i+'">候选段 '+(i+1)+' · '+x.start_s.toFixed(3)+'–'+x.end_s.toFixed(3)+' s<small>Frame '+x.start_frame+'–'+x.end_frame+' · '+x.frames+' 帧'+(seek?' · 点击定位':' · 无姿态视频可定位')+'</small></button>').join('')+'</div>':'<p class="action-note">旧版选择文件仅有汇总，缺少各段起止位置；不能定位分段，需新建任务生成明细。</p>')+(s.selected_count===0?'<p class="action-note">未选出候选动作段：姿态记录应为空，不能回退为全片推理。</p>':'');
 el.onclick=e=>{const button=e.target.closest('[data-action-segment]');if(button&&el.contains(button)){const segment=segments[Number(button.dataset.actionSegment)];if(segment&&seek)seek(segment);}};
}
`;
