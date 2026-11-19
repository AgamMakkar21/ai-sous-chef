import { useState, type FormEvent } from "react";
import { ApiError, type ReceiptResult } from "./api";
import { CartoonButton } from "@/components/ui/cartoon-button";

type Props = {
  disabled: boolean;
  onUpload: (file: File, key: string) => Promise<ReceiptResult>;
};

function failureMessage(error: unknown): string {
  if (!(error instanceof ApiError))
    return "The import could not be confirmed. Retry this same file to reconcile without adding it twice.";
  if (error.status === 401)
    return "Sign in again before importing your receipt.";
  if (error.status === 412)
    return "Your pantry changed during the import. Review the current list, then retry this same file.";
  if (error.status === 413)
    return "The receipt or resulting pantry is too large.";
  if (error.status === 422)
    return "Use a legible JPG, PNG, WebP, HEIC or PDF receipt (10 MiB, up to 3 PDF pages). Your pantry may also be full.";
  if (error.status === 503)
    return "Receipt reading is unavailable. You can still add groceries manually.";
  return "The import could not be confirmed. Retry this same file to reconcile without adding it twice.";
}

export default function ReceiptUpload({ disabled, onUpload }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [key, setKey] = useState("");
  const [reading, setReading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ReceiptResult | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file || reading || disabled) return;
    if (file.size === 0 || file.size > 10 * 1024 * 1024) {
      setError("Choose a nonempty receipt file of at most 10 MiB.");
      return;
    }
    setReading(true);
    setError("");
    setResult(null);
    try {
      setResult(await onUpload(file, key));
    } catch (failure) {
      setError(failureMessage(failure));
    } finally {
      setReading(false);
    }
  }

  return (
    <div className="receipt-upload">
      <details>
        <summary>Upload receipt</summary>
        <p id="receipt-help">
          AI adds food, skips nonfood and labels guessed amounts. Check the
          results: AI can misread receipts.
        </p>
        <form
          aria-label="Upload receipt"
          onSubmit={(event) => void submit(event)}
        >
          <fieldset
            className="form-fields receipt-file-row"
            disabled={disabled || reading}
          >
            <legend className="sr-only">Receipt import</legend>
            <div className="field">
              <label htmlFor="receipt-file" className="sr-only">
                Receipt file
              </label>
              <input
                id="receipt-file"
                type="file"
                required
                accept=".jpg,.jpeg,.png,.webp,.heic,.heif,.pdf"
                aria-describedby="receipt-help receipt-formats"
                onChange={(event) => {
                  setFile(event.currentTarget.files?.[0] ?? null);
                  setKey(crypto.randomUUID());
                  setError("");
                  setResult(null);
                }}
              />
            </div>
            <CartoonButton type="submit" disabled={!file}>
              Upload and add groceries
            </CartoonButton>
          </fieldset>
        </form>
        <p id="receipt-formats" className="field-help">
          JPG, PNG, WebP, HEIC or PDF. Up to 10 MiB and 3 PDF pages. Sent to
          Azure AI; images are not stored in your pantry.
        </p>
      </details>
      {reading && <p role="status">Reading receipt and adding food...</p>}
      {error && <p role="alert">{error}</p>}
      {result && (
        <div aria-label="Receipt import results" role="status">
          <p>
            {result.alreadyImported
              ? "This receipt was already imported. Nothing was added twice."
              : `Added ${result.added} foods, updated ${result.merged} pantry amounts, skipped ${result.skipped.length} nonfood lines. ${result.estimated} amounts estimated.`}
          </p>
          {result.skipped.length > 0 && (
            <details>
              <summary>Skipped nonfood items</summary>
              <ul>
                {result.skipped.map((item, index) => (
                  <li key={index}>{item.name}</li>
                ))}
              </ul>
            </details>
          )}
          {result.needsReview.length > 0 && (
            <>
              <h4>Needs review (not added)</h4>
              <ul>
                {result.needsReview.map((item, index) => (
                  <li key={index}>
                    {item.name}: {item.reason}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}
