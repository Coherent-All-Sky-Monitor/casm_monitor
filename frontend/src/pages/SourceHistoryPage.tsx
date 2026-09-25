import { useState } from "react";
import { FigureZoom } from "../components/FigureZoom";
import { Json, Notice, useResource } from "../components/Workspace";
import { SourceTransitHistory } from "../components/SourceTransitHistory";
import "../source-history.css";

const SOURCES = [
  {name:'B0329', aliases:['b0329','b0329+54','b032954']},
  {name:'Sun', aliases:['sun']},
  {name:'Cyg A', aliases:['cyga','cygnusa']},
  {name:'Cas A', aliases:['casa','cassiopeiaa']},
  {name:'Tau A', aliases:['taua','taurusa','crab']},
];

function HistoryDate({date, rows, all}: {date:string; rows:Json[]; all:boolean}) {
  const [image, setImage] = useState<Json|null>(null);
  const headline = rows.find(r => r.headline_artifact)?.headline_artifact;
  const plots = [...new Map(rows.flatMap(r => r.artifacts ?? []).map(a => [a.url, a])).values()] as Json[];
  const plot = (im:Json) => <button type="button" className="history-image" aria-label={`Enlarge ${im.name}`} onClick={() => setImage(im)}>
    <img loading="lazy" className="history-plot" src={im.url} alt={`B0329+54 · ${date} · ${im.name}`}/>
  </button>;
  return <article className="history-entry">
    <header><h3>{date}</h3>{all && <span className="muted">{rows.some(r => r.status === 'detection') ? 'Detection' : rows[0].status.replace(/_/g, ' ')}</span>}</header>
    {headline ? plot(headline) : <p className="empty-plot">Plot unavailable</p>}
    <details className="history-notes"><summary>Notes & saved plots ({plots.length})</summary>
      {rows.map(row => <section key={row.id}>
        {row.contains_retraction && <Notice>Includes a withdrawn claim; see notes below.</Notice>}
        <p>{row.outcome}</p><p className="muted">{row.config}</p><p className="muted">{row.directory}</p>
      </section>)}
      <p className="muted">Saved plots may include controls and unsuccessful folds.</p>
      {plots.map(im => <figure key={im.url}>{plot(im)}<figcaption><a href={im.url} target="_blank" rel="noreferrer">{im.name}</a></figcaption></figure>)}
    </details>
    {image && <FigureZoom title={`B0329+54 · ${date}`} onClose={() => setImage(null)}
      actions={<a href={image.url} target="_blank" rel="noreferrer">Open original PNG ↗</a>}>
      <img src={image.url} alt={image.name} draggable={false}/>
    </FigureZoom>}
  </article>;
}

function HistoryResults({search, filter, setFilter, active}: {search:string; filter:string; setFilter:(v:string)=>void;active:boolean}) {
  // Refresh only the small date catalogue. Finished figures are immutable for
  // their calibration/layout revision and do not get re-rendered by this poll.
  const {data, error} = useResource(`/api/sources?q=${encodeURIComponent(search)}`,active?300000:0);
  const attempts:Json[] = (data?.sources ?? []).flatMap((s:Json) => s.attempts ?? []);
  const shown = attempts.filter(a => filter === 'all' || a.status === 'detection');
  const days = new Map<string, Json[]>();
  for (const row of shown) {
    const date = row.date.slice(0,10);
    days.set(date, [...(days.get(date) ?? []), row]);
  }
  if (data?.kind === 'visibility_beam') return <>{error&&<Notice>Showing saved transits; the date list could not refresh. {error}</Notice>}<SourceTransitHistory data={data}/></>;
  return <>
    {error && <Notice>{error}</Notice>}
    {!data && !error && <Notice>Loading history…</Notice>}
    {!!attempts.length && <div className="history-result-heading"><p className="muted history-caption">B0329+54 · PDMP folds from filterbank dumps · {days.size} {filter === 'all' ? 'observation' : 'detection'} dates</p>
      <label>Show<select aria-label="Show" value={filter} onChange={e => setFilter(e.target.value)}>
        <option value="detections">Detections</option><option value="all">All observations</option>
      </select></label></div>}
    <div className="history-grid">{[...days].map(([date, rows]) => <HistoryDate key={`${filter}:${date}`} date={date} rows={rows} all={filter === 'all'}/>)}</div>
    {data && !shown.length && <Notice>{attempts.length ? 'No recorded detections.' : 'No saved history for this source.'}</Notice>}
  </>;
}

export default function SourceHistoryPage() {
  const [query,setQuery] = useState('B0329'), [search,setSearch] = useState('B0329'), [filter,setFilter] = useState('detections');
  const [visited,setVisited] = useState(['B0329']);
  const choose = (name:string) => {
    const alias=name.toLowerCase().replace(/[-_\s]/g,'');
    const canonical=SOURCES.find(s=>s.aliases.includes(alias))?.name??name;
    setQuery(canonical);setSearch(canonical);setVisited(old=>[...old.filter(name=>name!==canonical),canonical].slice(-8));
  };
  const selected = search.toLowerCase().replace(/[-_\s]/g,'');
  return <div className="workspace-page source-history-page">
    <div className="page-heading"><h2>Source history</h2></div>
    <div className="source-selector" role="group" aria-label="Choose source">
      {SOURCES.map(({name,aliases})=><button key={name} type="button" aria-pressed={aliases.includes(selected)}
        onClick={()=>choose(name)}>{name}</button>)}
    </div>
    <form className="field-row control-surface" onSubmit={e => {e.preventDefault(); choose(query.trim());}}>
      <label>Source<input type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="Source name or alias"/></label>
      <button className="primary">Search</button>
    </form>
    {visited.map(name=><section key={name} hidden={name!==search}>
      <HistoryResults search={name} active={name===search} filter={filter} setFilter={setFilter}/>
    </section>)}
  </div>;
}
