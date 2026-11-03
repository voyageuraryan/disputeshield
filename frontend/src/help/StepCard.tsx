import { useEffect, useRef, useState } from "react";
import { ApiError, helpApi, type BuyerSession, type ProofUpload, type StepAnswer, type TSSourceView, type TSStepView } from "../api";
import { STEP_STATUS_LABEL, VIDEO_MAX_SECONDS, checkFile, domainOf, stepActions } from "./logic";

export const btn =
  "inline-flex min-h-[44px] items-center justify-center rounded-lg px-4 py-2 text-base font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50";

const KIND: Record<string, string> = { photo: "photo", video: "video", screenshot: "screenshot", none: "photo" };
const STATUS_STYLE: Record<TSStepView["status"], string> = {
  PENDING: "bg-slate-100 text-slate-700",
  DONE_FIXED: "bg-emerald-100 text-emerald-900",
  DONE_NOT_FIXED: "bg-slate-200 text-slate-800",
  SKIPPED: "bg-slate-100 text-slate-700",
  CANT_DO: "bg-amber-100 text-amber-900",
};

export type StepCardProps = {
  step: TSStepView;
  sources: TSSourceView[];
  open: boolean; // session still running
  busy: boolean;
  current: boolean;
  sessionId: string;
  token: string;
  onAnswer: (status: StepAnswer, note: string) => void;
  onUploaded: (r: ProofUpload) => void;
  onChanged?: (s: BuyerSession) => void; // after the buyer deletes an upload
};

export function StepCard({ step: s, sources, open, busy, current, sessionId, token, onAnswer, onUploaded, onChanged }: StepCardProps) {
  const a = stepActions(s, open, busy);
  const [note, setNote] = useState("");
  const [showNote, setShowNote] = useState(false);
  const kind = KIND[s.proof_kind] ?? "photo";
  const webSources = sources.filter((x) => /^https?:\/\//.test(x.url));
  return (
    <li
      aria-current={current ? "step" : undefined}
      className={`rounded-xl border bg-white p-4 ${current ? "border-indigo-400 ring-1 ring-indigo-200" : "border-slate-200"}`}
    >
      <div className="flex items-start gap-3">
        <span aria-hidden className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold ${s.status === "PENDING" ? "bg-indigo-700 text-white" : "bg-slate-200 text-slate-700"}`}>
          {s.status === "PENDING" ? s.ordinal : "✓"}
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="font-semibold leading-snug">
            <span className="sr-only">Step {s.ordinal}: </span>
            {s.title}
          </h3>
          <div className="mt-1 flex flex-wrap gap-1.5 text-xs font-medium">
            {s.safety_level === "caution" ? (
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-900">⚠ Take care: power or heat</span>
            ) : (
              <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-emerald-800">Safe step</span>
            )}
            {s.requires_proof && <span className="rounded-full bg-sky-50 px-2 py-0.5 text-sky-900">Needs a {kind}</span>}
            {s.status !== "PENDING" && <span className={`rounded-full px-2 py-0.5 ${STATUS_STYLE[s.status]}`}>{STEP_STATUS_LABEL[s.status]}</span>}
          </div>
        </div>
      </div>
      <p className="mt-3 text-base text-slate-800">{s.instruction}</p>
      {s.why && (
        <p className="mt-2 text-sm text-slate-600">
          <span className="font-medium text-slate-700">Why this helps: </span>
          {s.why}
        </p>
      )}
      {webSources.length > 0 && (
        <p className="mt-2 text-sm text-slate-600">
          Source:{" "}
          {webSources.map((x, i) => (
            <span key={x.id}>
              {i > 0 && ", "}
              <a href={x.url} target="_blank" rel="noopener noreferrer nofollow" className="text-indigo-800 underline">
                {domainOf(x.url) || x.title || "link"}
              </a>
            </span>
          ))}
        </p>
      )}
      {s.buyer_note && <p className="mt-2 text-sm text-slate-600">Your note: {s.buyer_note}</p>}

      {a.canUpload && (
        <UploadArea step={s} kind={kind} sessionId={sessionId} token={token} highlight={a.needsProofNow} onUploaded={onUploaded} onChanged={onChanged} />
      )}

      {a.canAnswer && (
        <div className="mt-4 space-y-2">
          {showNote ? (
            <div>
              <label htmlFor={`note-${s.id}`} className="text-sm font-medium text-slate-700">
                Note for this step (optional)
              </label>
              <input
                id={`note-${s.id}`}
                maxLength={500}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                className="mt-1 block w-full rounded-lg border border-slate-400 px-3 py-2 text-base focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-600"
              />
            </div>
          ) : (
            <button type="button" onClick={() => setShowNote(true)} className="text-sm font-medium text-indigo-800 underline">
              Add a note
            </button>
          )}
          <div className="grid gap-2 sm:grid-cols-3">
            <button type="button" disabled={busy} onClick={() => onAnswer("DONE_FIXED", note)} className={`${btn} bg-emerald-700 text-white hover:bg-emerald-800`}>
              Done, it works now
            </button>
            <button type="button" disabled={busy} onClick={() => onAnswer("DONE_NOT_FIXED", note)} className={`${btn} border border-slate-400 bg-white text-slate-900 hover:bg-slate-50`}>
              Done, still not working
            </button>
            <button type="button" disabled={busy} onClick={() => onAnswer("CANT_DO", note)} className={`${btn} text-slate-700 underline hover:bg-slate-50`}>
              I can't do this
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

type UploadState = { file: File; preview: string | null; progress: number; error: string | null; uploading: boolean };

function videoSeconds(file: File): Promise<number | null> {
  return new Promise((resolve) => {
    if (typeof document === "undefined" || typeof URL.createObjectURL !== "function") return resolve(null);
    const v = document.createElement("video");
    const url = URL.createObjectURL(file);
    const done = (n: number | null) => {
      URL.revokeObjectURL(url);
      resolve(n);
    };
    const t = setTimeout(() => done(null), 3000); // some phones' formats can't be read here: the server checks
    v.preload = "metadata";
    v.onloadedmetadata = () => {
      clearTimeout(t);
      done(Number.isFinite(v.duration) ? v.duration : null);
    };
    v.onerror = () => {
      clearTimeout(t);
      done(null);
    };
    v.src = url;
  });
}

export function UploadArea({
  step,
  kind,
  sessionId,
  token,
  highlight,
  onUploaded,
  onChanged,
}: {
  step: TSStepView;
  kind: string;
  sessionId: string;
  token: string;
  highlight: boolean;
  onUploaded: (r: ProofUpload) => void;
  onChanged?: (s: BuyerSession) => void;
}) {
  const [up, setUp] = useState<UploadState | null>(null);
  const live = useRef<string | null>(null);
  useEffect(() => () => void (live.current && URL.revokeObjectURL(live.current)), []);

  const start = async (file: File, retry = false) => {
    const problem = checkFile(file);
    if (!retry && !problem && (file.type.startsWith("video/") || /\.(mp4|mov)$/i.test(file.name))) {
      const secs = await videoSeconds(file);
      if (secs && secs > VIDEO_MAX_SECONDS + 0.5) {
        setUp({ file, preview: null, progress: 0, uploading: false, error: `Please keep the video under ${VIDEO_MAX_SECONDS} seconds (this one is ${Math.round(secs)} s).` });
        return;
      }
    }
    let preview = up?.file === file ? up.preview : null;
    if (!preview && file.type.startsWith("image/") && !/hei[cf]/i.test(file.type) && typeof URL.createObjectURL === "function") {
      if (live.current) URL.revokeObjectURL(live.current);
      preview = live.current = URL.createObjectURL(file);
    }
    if (problem) return setUp({ file, preview, progress: 0, uploading: false, error: problem });
    setUp({ file, preview, progress: 0, uploading: true, error: null });
    try {
      const r = await helpApi.uploadProof(sessionId, token, step.id, file, (pct) => setUp((u) => (u ? { ...u, progress: pct } : u)));
      setUp((u) => (u ? { ...u, uploading: false, progress: 100 } : u));
      onUploaded(r);
    } catch (e) {
      const msg = (e as Error).message.replace(/^\d+: /, "");
      setUp((u) => (u ? { ...u, uploading: false, error: e instanceof ApiError && e.status === 0 ? msg : msg || "The upload failed." } : u));
    }
  };
  const pick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (f) start(f);
  };
  const id = `up-${step.id}`;
  return (
    <div className={`mt-4 rounded-lg border-2 border-dashed p-3 ${highlight ? "border-indigo-500 bg-indigo-50" : "border-slate-300 bg-slate-50"}`}>
      <p className="text-sm font-medium text-slate-800">
        {step.proofs > 0 ? `Uploaded ✓ (${step.proofs})` : `Add a ${kind} for this step`}
        <span className="block font-normal text-slate-600">Photos up to 10 MB, videos up to 30 seconds. We remove location data.</span>
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        <input id={`${id}-cam`} type="file" accept="image/*,video/*" capture="environment" className="peer/cam sr-only" onChange={pick} disabled={up?.uploading} aria-label={`Use the camera for step ${step.ordinal}`} />
        <label htmlFor={`${id}-cam`} className={`${btn} cursor-pointer bg-indigo-700 text-white hover:bg-indigo-800 peer-focus-visible/cam:ring-2 peer-focus-visible/cam:ring-indigo-600 peer-focus-visible/cam:ring-offset-2`}>
          Use camera
        </label>
        <input id={`${id}-file`} type="file" accept="image/*,video/*,.heic,.heif" className="peer/file sr-only" onChange={pick} disabled={up?.uploading} aria-label={`Upload a ${kind} for step ${step.ordinal}`} />
        <label htmlFor={`${id}-file`} className={`${btn} cursor-pointer border border-slate-400 bg-white text-slate-900 hover:bg-slate-50 peer-focus-visible/file:ring-2 peer-focus-visible/file:ring-indigo-600 peer-focus-visible/file:ring-offset-2`}>
          Choose a file
        </label>
      </div>
      {(step.proof_files?.length ?? 0) > 0 && (
        <ul className="mt-3 flex flex-wrap gap-2" aria-label="Your uploads for this step">
          {step.proof_files!.map((f) => (
            <ProofThumb key={f.id} sessionId={sessionId} token={token} proofId={f.id} video={f.kind === "video"} onChanged={onChanged} />
          ))}
        </ul>
      )}
      {up && (
        <div className="mt-3 flex items-center gap-3" aria-live="polite">
          {up.preview ? (
            <img src={up.preview} alt="" className="h-14 w-14 rounded-md object-cover" />
          ) : (
            <span aria-hidden className="flex h-14 w-14 items-center justify-center rounded-md bg-slate-200 text-xl">{/video|mp4|mov/i.test(up.file.type + up.file.name) ? "🎬" : "🖼"}</span>
          )}
          <div className="min-w-0 flex-1 text-sm">
            <p className="truncate text-slate-800">{up.file.name || "Your file"}</p>
            {up.uploading && (
              <div role="progressbar" aria-label="Upload progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={up.progress} className="mt-1 h-2 w-full overflow-hidden rounded-full bg-slate-200">
                <div className="h-full bg-indigo-700 transition-all" style={{ width: `${up.progress}%` }} />
              </div>
            )}
            {!up.uploading && !up.error && up.progress === 100 && <p className="text-emerald-800">Uploaded</p>}
            {up.error && (
              <p role="alert" className="text-red-800">
                {up.error}{" "}
                {!checkFile(up.file) && !/seconds/.test(up.error) && (
                  <button type="button" onClick={() => start(up.file, true)} className="font-medium underline">
                    Retry
                  </button>
                )}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** A photo thumbnail is fetched with the link headers (an <img> can't send them); videos (up to 50 MB) are
 * shown as a tile instead of being downloaded again just for a preview. */
function ProofThumb({
  sessionId,
  token,
  proofId,
  video,
  onChanged,
}: {
  sessionId: string;
  token: string;
  proofId: number;
  video: boolean;
  onChanged?: (s: BuyerSession) => void;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const remove = async () => {
    if (!window.confirm(`Delete this ${video ? "video" : "photo"}? It can't be undone.`)) return;
    try {
      onChanged?.(await helpApi.deleteProof(sessionId, token, proofId));
    } catch (e) {
      setErr((e as Error).message.replace(/^\d+: /, ""));
    }
  };
  useEffect(() => {
    if (video) return;
    let url: string | null = null;
    let alive = true;
    helpApi
      .proofBlob(sessionId, token, proofId)
      .then((b) => {
        if (!alive || typeof URL.createObjectURL !== "function") return;
        url = URL.createObjectURL(b);
        setSrc(url);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [sessionId, token, proofId, video]);
  return (
    <li className="relative flex h-16 w-16 items-center justify-center overflow-hidden rounded-md bg-slate-200">
      {video ? (
        <span role="img" aria-label="Your video" className="text-2xl">🎬</span>
      ) : src ? (
        <img src={src} alt="Your photo" className="h-full w-full object-cover" />
      ) : (
        <span className="sr-only">Your photo</span>
      )}
      {onChanged && (
        <button
          type="button"
          onClick={remove}
          aria-label={`Delete this ${video ? "video" : "photo"}`}
          className="absolute right-0.5 top-0.5 flex h-6 w-6 items-center justify-center rounded-full bg-white/90 text-sm font-semibold text-slate-800 shadow focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-600"
        >
          ×
        </button>
      )}
      {err && (
        <span role="alert" className="sr-only">
          {err}
        </span>
      )}
    </li>
  );
}
