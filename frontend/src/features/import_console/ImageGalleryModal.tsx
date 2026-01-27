import React from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

type Props = { open: boolean; onClose: () => void; title?: string; images: string[] };

export default function ImageGalleryModal({ open, onClose, images, title }: Props) {
  const [idx, setIdx] = React.useState(0);
  React.useEffect(() => { if (!open) setIdx(0); }, [open]);

  if (!images?.length) return null;
  const next = () => setIdx((i) => (i + 1) % images.length);
  const prev = () => setIdx((i) => (i - 1 + images.length) % images.length);

  return (
    <Dialog open={open} onOpenChange={(v) => (!v ? onClose() : null)}>
      <DialogContent className="max-w-4xl bg-neutral-900 text-neutral-100">
        <DialogHeader>
          <DialogTitle>{title || "Images"}</DialogTitle>
        </DialogHeader>
        <div className="flex items-center gap-4">
          <button className="px-3 py-2 rounded-xl border" onClick={prev}>&larr;</button>
          <div className="flex-1">
            <img src={images[idx]} alt={`img-${idx}`} className="w-full max-h-[70vh] object-contain rounded-xl" />
            <div className="text-center text-sm mt-2">{idx + 1} / {images.length}</div>
          </div>
          <button className="px-3 py-2 rounded-xl border" onClick={next}>&rarr;</button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
