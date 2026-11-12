import { Suspense } from "react";
import { DevicePage } from "../../views/DevicePage";

// /device?kind=…&id=…: the entry is read from the address in the browser (see routes.ts).
export default function Page() {
  return (
    <Suspense
      fallback={
        <div className="page device-page">
          <p className="hint" role="status">
            Loading…
          </p>
        </div>
      }
    >
      <DevicePage />
    </Suspense>
  );
}
