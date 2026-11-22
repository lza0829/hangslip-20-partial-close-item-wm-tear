import { useEffect, useState } from "react";
import { api } from "../api";

// 全局口径：票籍听件。
//   勾选件 → 取走 → 未取齐即「部分已取」，仍留在待取页；
//   取齐才关票进「已取」，水位同步扣减。
export default function Ready() {
  const [rows, setRows] = useState([]);
  const [codes, setCodes] = useState({});
  const [sel, setSel] = useState({});
  const [msg, setMsg] = useState("");
  const [ok, setOk] = useState(false);

  async function load() {
    setRows(await api.待取列表());
  }

  useEffect(() => {
    load().catch((e) => setMsg(e.message));
  }, []);

  function toggle(ticket_no, item, checked) {
    setSel((prev) => ({
      ...prev,
      [ticket_no]: {
        ...(prev[ticket_no] || {}),
        [item.id]: checked ? item.remaining : 0,
      },
    }));
  }

  function setQty(ticket_no, item, value) {
    const n = Math.max(0, Math.min(item.remaining, Number(value) || 0));
    setSel((prev) => ({
      ...prev,
      [ticket_no]: { ...(prev[ticket_no] || {}), [item.id]: n },
    }));
  }

  async function pick(ticket_no) {
    setMsg("");
    setOk(false);
    try {
      const chosen = Object.entries(sel[ticket_no] || {})
        .filter(([, qty]) => qty > 0)
        .map(([item_id, qty]) => ({ item_id: Number(item_id), qty }));
      const row = await api.取衣(ticket_no, {
        pickup_code: codes[ticket_no] || "",
        operator: "柜员",
        ...(chosen.length ? { items: chosen } : {}),
      });
      setOk(true);
      if (row.status === "已取") {
        setMsg(`${ticket_no} 全部取齐，已关票（${row.picked_qty}/${row.total_qty} 件）`);
      } else {
        setMsg(
          `${ticket_no} 部分取走，仍余 ${row.remaining_qty} 件，票态「${row.status}」`
        );
      }
      setSel((prev) => ({ ...prev, [ticket_no]: {} }));
      await load();
    } catch (e) {
      setMsg(e.message);
    }
  }

  // 未勾任何件＝取走全部在册残留件。
  function allRemaining(ticket_no, r) {
    return Object.values(sel[ticket_no] || {}).every((q) => !q) ? `全部 ${r.remaining_qty} 件` : "";
  }

  return (
    <section className="panel">
      <h3>待取核销</h3>
      <p className={`msg ${ok ? "ok" : ""}`}>{msg}</p>
      <table>
        <thead>
          <tr>
            <th>票号</th>
            <th>顾客</th>
            <th>承诺日</th>
            <th>票态</th>
            <th>件数（已取/在册）</th>
            <th>按件勾选</th>
            <th>暗码</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.ticket_no}>
              <td>{r.ticket_no}</td>
              <td>{r.customer_name}</td>
              <td>{r.due_date}</td>
              <td>
                <span className={`badge ${r.status}`}>{r.status}</span>
              </td>
              <td>
                {r.picked_qty}/{r.total_qty}
              </td>
              <td>
                <div className="row">
                  {(r.remaining_items || r.items)
                    .filter((i) => i.remaining > 0)
                    .map((i) => (
                      <label key={i.id} className="pick-item">
                        <input
                          type="checkbox"
                          checked={(sel[r.ticket_no]?.[i.id] || 0) > 0}
                          onChange={(e) => toggle(r.ticket_no, i, e.target.checked)}
                        />
                        {i.garment} ×{i.remaining}
                        {(sel[r.ticket_no]?.[i.id] || 0) > 0 && i.remaining > 1 && (
                          <input
                            type="number"
                            min="1"
                            max={i.remaining}
                            className="qty"
                            value={sel[r.ticket_no][i.id]}
                            onChange={(e) => setQty(r.ticket_no, i, e.target.value)}
                          />
                        )}
                      </label>
                    ))}
                </div>
              </td>
              <td>
                <input
                  placeholder="输入暗码"
                  value={codes[r.ticket_no] || ""}
                  onChange={(e) =>
                    setCodes({ ...codes, [r.ticket_no]: e.target.value })
                  }
                />
              </td>
              <td>
                <button type="button" onClick={() => pick(r.ticket_no)}>
                  {allRemaining(r.ticket_no, r) ? "全部核销" : "按件核销"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
