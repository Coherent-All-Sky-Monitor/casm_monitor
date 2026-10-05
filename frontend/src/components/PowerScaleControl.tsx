import {PowerScale} from './powerScale';
import '../power-scale.css';

export function PowerScaleControl({value,onChange,label='Power scale'}:{value:PowerScale;onChange:(value:PowerScale)=>void;label?:string}){
  return <div className="choice-row power-scale-control" role="group" aria-label={label}>
    {([['db','Power (dB)'],['linear','Linear']] as const).map(([id,name])=><button type="button" key={id} className={value===id?'active':''} aria-pressed={value===id} onClick={()=>onChange(id)}>{name}</button>)}
  </div>;
}
