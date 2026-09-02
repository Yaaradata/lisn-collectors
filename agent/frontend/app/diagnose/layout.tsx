import { DiagnoseNav } from "@/components/layout/DiagnoseNav";

export default function DiagnoseLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div>
      <DiagnoseNav />
      {children}
    </div>
  );
}
