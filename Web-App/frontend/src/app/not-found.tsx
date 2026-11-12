import { PageNotFound } from "../components/PageNotFound";

// Any other address (exported as 404.html).
export default function NotFound() {
  return <PageNotFound message="This page doesn't exist." />;
}
