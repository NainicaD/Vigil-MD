import Link from "next/link";
import { paths } from "../routes";

/** "Page not found": an unknown address, or a device link whose entry doesn't exist. */
export function PageNotFound({
  inline = false,
  message = "This entry doesn't exist.",
}: {
  inline?: boolean;
  message?: string;
}) {
  return (
    <div className={inline ? undefined : "page"}>
      <h1>Page not found</h1>
      <p>
        {message} <Link href={paths.search()}>Search again</Link>.
      </p>
    </div>
  );
}
