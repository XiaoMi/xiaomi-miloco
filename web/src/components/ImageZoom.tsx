/** 缩略图 + 点击放大查看：点击缩略图弹全屏遮罩看原图，点击遮罩或 ESC 关闭。 */
import { useState } from "react";
import { useEscClose } from "@/hooks/useEscClose";

export function ImageZoom({
  src,
  alt = "",
  thumbClass = "",
}: {
  src: string;
  alt?: string;
  thumbClass?: string;
}) {
  const [open, setOpen] = useState(false);
  useEscClose(open, () => setOpen(false));
  return (
    <>
      <img
        src={src}
        alt={alt}
        onClick={() => setOpen(true)}
        className={`cursor-zoom-in ${thumbClass}`}
      />
      {open && (
        <div
          className="fixed inset-0 z-[90] flex items-center justify-center bg-black/75 p-6 cursor-zoom-out"
          onClick={(e) => {
            e.stopPropagation();
            setOpen(false);
          }}
        >
          <img
            src={src}
            alt={alt}
            className="max-h-full max-w-full rounded-lg object-contain"
          />
        </div>
      )}
    </>
  );
}
