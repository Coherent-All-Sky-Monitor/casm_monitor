/** Local figure export. No new science requests, uploads, or third-party service. */
const SVG_NS='http://www.w3.org/2000/svg';

export function figureFilename(title:string, extension:string):string {
  const stem=title.normalize('NFKD').replace(/[\u0300-\u036f]/g,'')
    .replace(/[^a-zA-Z0-9_-]+/g,'-').replace(/^-+|-+$/g,'').slice(0,140)||'figure';
  return `${stem}.${extension}`;
}

function save(blob:Blob, name:string) {
  const url=URL.createObjectURL(blob),link=document.createElement('a');
  link.href=url;link.download=name;document.body.append(link);link.click();link.remove();
  // Leave time for the browser to consume the object URL before releasing it.
  window.setTimeout(()=>URL.revokeObjectURL(url),30000);
}

async function imageBlob(src:string):Promise<Blob> {
  const url=new URL(src,location.href);
  if(!['data:','blob:'].includes(url.protocol)&&url.origin!==location.origin)
    throw new Error('Cannot download an image from another site.');
  const response=await fetch(url.href);
  if(!response.ok)throw new Error(`Image download failed (${response.status}).`);
  const blob=await response.blob();
  if(!blob.type.startsWith('image/'))throw new Error('The saved figure is unavailable.');
  return blob;
}

function dataUrl(blob:Blob):Promise<string> {
  return new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onload=()=>resolve(String(reader.result));reader.onerror=()=>reject(new Error('Cannot read figure image.'));
    reader.readAsDataURL(blob);
  });
}

export async function downloadFigure(content:HTMLElement, title:string):Promise<void> {
  await document.fonts.ready;
  const images=Array.from(content.querySelectorAll('img'));
  await Promise.all(images.map(img=>img.decode()));
  // Saved PNGs/photos retain their original resolution and bytes, even when zoomed.
  if(content.childElementCount===1&&content.firstElementChild instanceof HTMLImageElement) {
    const blob=await imageBlob(content.firstElementChild.currentSrc||content.firstElementChild.src);
    const extension=({'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/svg+xml':'svg'} as Record<string,string>)[blob.type]??'png';
    save(blob,figureFilename(title,extension));return;
  }

  // Snapshot the entire figure, including DOM axes, colourbars, SVG curves and
  // companion panels. Capturing only the waterfall <img> would lose its axes.
  const width=Math.ceil(content.getBoundingClientRect().width),height=Math.ceil(content.scrollHeight);
  if(width<1||height<1)throw new Error('The figure is not ready yet.');
  const clone=content.cloneNode(true) as HTMLElement;
  const originals=[content,...content.querySelectorAll('*')];
  const copies=[clone,...clone.querySelectorAll('*')];
  const embedded=new Map<string,Promise<string>>();
  const pending:Promise<void>[]=[];
  originals.forEach((original,i)=>{
    const copy=copies[i] as HTMLElement|SVGElement,style=getComputedStyle(original);
    for(const property of Array.from(style)) {
      // SVG clip paths must remain local inside the exported document.
      const value=style.getPropertyValue(property).replace(/url\(["']?[^)"']*#([^)'" ]+)["']?\)/g,'url(#$1)');
      copy.style.setProperty(property,value);
    }
    copy.style.setProperty('animation','none');copy.style.setProperty('transition','none');
    if(original instanceof HTMLImageElement) {
      const src=original.currentSrc||original.src;
      if(!embedded.has(src))embedded.set(src,imageBlob(src).then(dataUrl));
      pending.push(embedded.get(src)!.then(url=>{
        const img=copy as HTMLImageElement;img.removeAttribute('srcset');img.removeAttribute('sizes');
        img.loading='eager';img.src=url;
      }));
    } else if(original instanceof HTMLCanvasElement) {
      const img=document.createElement('img');img.src=original.toDataURL('image/png');
      img.style.cssText=copy.style.cssText;copy.replaceWith(img);
    }
  });
  await Promise.all(pending);
  clone.style.width=`${width}px`;clone.style.height=`${height}px`;clone.style.minWidth='0';
  clone.style.maxHeight='none';clone.style.overflow='visible';clone.style.background='white';
  // Transit labels can sit just above the plot's layout box. Leave a margin
  // inside the image viewport so they are not cut at the foreignObject edge.
  const outputWidth=width+48,outputHeight=height+48;
  const wrapper=document.createElement('div');
  wrapper.style.cssText=`box-sizing:border-box;width:${outputWidth}px;height:${outputHeight}px;padding:24px;background:white;`;
  wrapper.append(clone);
  const svg=document.createElementNS(SVG_NS,'svg');
  svg.setAttribute('width',String(outputWidth));svg.setAttribute('height',String(outputHeight));
  const foreign=document.createElementNS(SVG_NS,'foreignObject');
  foreign.setAttribute('width','100%');foreign.setAttribute('height','100%');
  foreign.append(wrapper);svg.append(foreign);
  const picture=new Image();
  picture.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(new XMLSerializer().serializeToString(svg));
  await picture.decode();
  // High resolution, with a bounded pixel budget even at 400% display zoom.
  const header=48,scale=Math.min(2,8192/outputWidth,8192/(outputHeight+header),Math.sqrt(16_000_000/(outputWidth*(outputHeight+header))));
  const canvas=document.createElement('canvas');
  canvas.width=Math.ceil(outputWidth*scale);canvas.height=Math.ceil((outputHeight+header)*scale);
  const context=canvas.getContext('2d');
  if(!context)throw new Error('Image export is unavailable in this browser.');
  context.scale(scale,scale);context.fillStyle='white';context.fillRect(0,0,outputWidth,outputHeight+header);
  context.fillStyle='#253247';context.font='bold 16px Arial, sans-serif';context.fillText(title,24,29,outputWidth-48);
  context.drawImage(picture,0,header,outputWidth,outputHeight);
  const blob=await new Promise<Blob>((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(new Error('Cannot encode figure PNG.')),'image/png'));
  save(blob,figureFilename(title,'png'));
}
