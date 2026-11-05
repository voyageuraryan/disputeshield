import { useEffect, useState, type ReactNode } from "react";
import { fetchAuthedBlob, openAuthed } from "./api";

/** A link to a merchant-only file (evidence PDF, upload, buyer proof): opened with the session header, never with
 * the token in the URL (security review 2026-11-05). */
export function AuthedLink({ href, className, children }: { href: string; className?: string; children: ReactNode }) {
  return (
    <a
      href={href}
      className={className}
      onClick={(e) => {
        e.preventDefault();
        openAuthed(href);
      }}
    >
      {children}
    </a>
  );
}

/** An <img> for a merchant-only image: fetched with the session header and shown from a blob: URL. */
export function AuthedImg({ src, alt, className }: { src: string; alt: string; className?: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let u: string | null = null;
    let live = true;
    fetchAuthedBlob(src)
      .then((b) => {
        if (!live) return;
        u = URL.createObjectURL(b);
        setUrl(u);
      })
      .catch(() => undefined);
    return () => {
      live = false;
      if (u) URL.revokeObjectURL(u);
    };
  }, [src]);
  return url ? <img src={url} alt={alt} className={className} /> : <span className={`${className ?? ""} inline-block bg-slate-100`} aria-label={alt} />;
}
