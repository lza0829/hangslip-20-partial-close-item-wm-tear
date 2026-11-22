import { useEffect, useState } from "react";
import { api } from "../api";

// 残留列表：件级残留独立可核验。
// 不按票态过滤——票进「部分已取」或转隔离，未取件仍在此可见。
export default function Residual() {
  const [rows, setRows] = useState([]);
  const [all, setAll] = useState(false);
  const [msg, setMsg] = useState("");

  async function load(includeAll) {
    setRows(await api.残留列表(includeAll ? "?include_in_process=true" : ""));
  }

  useEffect(() => {
    load(false).catch((e) => setMsg(e.message));
  }, []);

  function toggleAll() {
    const next = !all;
    setAll(next);
    load(next).catch((e) => setMsg(e.message));
  }

  const total = rows.reduce((s, r) => s + r.remaining, 0);

  return (
    <section className="panel">
      <h3>残留件核验</h3>
      <p className="msg">{msg}</p>
      <div className="row">
        <div className="metric">
          <b>{total}</b>
          <span>在册残留件</span>
        </div>
        <div className="metric">
          <b>{new Set(rows.map((r) => r.ticket_no)).size}</b>
          <span>涉及票数</span>
        </div>
        <button type="button" className="ghost" onClick={toggleAll}>
          {all ? "只看取衣后残留" : "含工序中在册件"}
        </button>
      </div>
      <table>
        <thead>
          <tr>
            <th>票号</th>
            <th>顾客</th>
            <th>票态</th>
            <th>衣物</th>
            <th>在册</th>
            <th>已取</th>
            <th>残留</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.item_id}>
              <td>{r.ticket_no}</td>
              <td>{r.customer_name}</td>
              <td>
                <span className={`badge ${r.ticket_status}`}>{r.ticket_status}</span>
              </td>
              <td>{r.garment}</td>
              <td>{r.qty}</td>
              <td>{r.picked_qty}</td>
              <td>
                <b>{r.remaining}</b>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
