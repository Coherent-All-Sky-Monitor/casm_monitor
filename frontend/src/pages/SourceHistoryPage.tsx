import { useState } from "react";
import { FigureZoom } from "../components/FigureZoom";
import { Json, Notice, useResource } from "../components/Workspace";
import { SourceTransitHistory } from "../components/SourceTransitHistory";
import "../source-history.css";

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

// Keying results prevents a previous source staying on screen during a search.
function HistoryResults({search, filter, setFilter}: {search:string; filter:string; setFilter:(v:string)=>void}) {
  const {data, error} = useResource(`/api/sources?q=${encodeURIComponent(search)}`);
  const attempts:Json[] = (data?.sources ?? []).flatMap((s:Json) => s.attempts ?? []);
  const shown = attempts.filter(a => filter === 'all' || a.status === 'detection');
  const days = new Map<string, Json[]>();
  for (const row of shown) {
    const date = row.date.slice(0,10);
    days.set(date, [...(days.get(date) ?? []), row]);
  }
  if (data?.kind === 'visibility_beam') return <SourceTransitHistory data={data}/>;
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
  const [refresh,setRefresh] = useState(0);
  const choose = (name:string) => {setQuery(name); setSearch(name); setRefresh(v=>v+1);};
  return <div className="workspace-page source-history-page">
    <div className="page-heading"><h2>Source history</h2></div>
    <form className="field-row control-surface" onSubmit={e => {e.preventDefault(); choose(query.trim());}}>
      <label>Source<input type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="B0329, Sun or Cyg A"/></label>
      <button className="primary">Search</button>
      <div className="source-shortcuts">{['B0329','Sun','Cyg A'].map(name=><button key={name} type="button" onClick={()=>choose(name)}>{name}</button>)}</div>
    </form>
    <HistoryResults key={`${search}:${refresh}`} search={search} filter={filter} setFilter={setFilter}/>
  </div>;
}
