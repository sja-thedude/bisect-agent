"use client";
import { Highlight, themes } from "prism-react-renderer";

/** Unified diff with per-line Python syntax highlighting and +/- gutters. */
export function DiffView({ diff, maxHeight = 520 }: { diff: string; maxHeight?: number }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <div className="overflow-auto scroll-thin rounded-lg border border-line bg-[#0d1117] font-mono text-[12.5px] leading-[1.55]" style={{ maxHeight }}>
      <table className="w-full border-collapse">
        <tbody>
          {lines.map((line, i) => {
            const meta = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ");
            const hunk = line.startsWith("@@");
            const add = !meta && line.startsWith("+");
            const del = !meta && line.startsWith("-");
            const body = add || del || line.startsWith(" ") ? line.slice(1) : line;
            const bg = add ? "bg-[#12261e]" : del ? "bg-[#2d1517]" : hunk ? "bg-[#161b33]" : "";
            return (
              <tr key={i} className={bg}>
                <td className={`select-none w-6 pl-2 pr-1 text-right ${add ? "text-good" : del ? "text-bad" : "text-muted"}`}>
                  {add ? "+" : del ? "−" : ""}
                </td>
                <td className="whitespace-pre pr-4">
                  {meta || hunk ? (
                    <span className={hunk ? "text-[#a5b4fc]" : "text-muted font-medium"}>{line}</span>
                  ) : (
                    <Highlight code={body || " "} language="python" theme={themes.vsDark}>
                      {({ tokens, getTokenProps }) => (
                        <>{tokens[0]?.map((t, k) => <span key={k} {...getTokenProps({ token: t })} style={{ ...getTokenProps({ token: t }).style, background: "transparent" }} />)}</>
                      )}
                    </Highlight>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function CodeView({ code, maxHeight = 360 }: { code: string; maxHeight?: number }) {
  return (
    <Highlight code={code.replace(/\n$/, "")} language="python" theme={themes.vsDark}>
      {({ tokens, getLineProps, getTokenProps }) => (
        <pre className="overflow-auto scroll-thin rounded-lg border border-line bg-[#0d1117] p-3 font-mono text-[12.5px] leading-[1.55]" style={{ maxHeight }}>
          {tokens.map((line, i) => (
            <div key={i} {...getLineProps({ line })}>
              <span className="mr-3 inline-block w-6 select-none text-right text-muted/60">{i + 1}</span>
              {line.map((t, k) => <span key={k} {...getTokenProps({ token: t })} />)}
            </div>
          ))}
        </pre>
      )}
    </Highlight>
  );
}
