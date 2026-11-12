import { Suspense } from "react";
import { SearchPage } from "../views/SearchPage";

// The search reads ?q= from the address, which only the browser knows in a static site.
export default function Page() {
  return (
    <Suspense fallback={null}>
      <SearchPage />
    </Suspense>
  );
}
