Feature: Dinh danh nguoi chi giang vien co trong danh muc
  Bao criteria AC-C1 den AC-C7: chinh xac dat tren het, khong bia ten.

  Scenario: Nguoi la khong duoc dinh danh
    Given khuon mat khong khop ai trong danh muc giang vien
    When dinh danh
    Then ten de trong
    And khong luu embedding khuon mat do

  Scenario: Diem thap bi loai
    Given diem khop duoi nguong thap da hieu chuan
    When dinh danh
    Then ten de trong

  Scenario: Diem lung chung day sang duyet tay
    Given diem nam giua 2 nguong da hieu chuan
    When dinh danh
    Then dua vao hang doi duyet tay
    And khong tu dong gan ten

  Scenario: Diem cao nhung sat nut ung vien thu hai thi duyet tay
    Given diem cao nhat 0.91 va diem thu hai 0.90
    When dinh danh
    Then dua vao hang doi duyet tay

  Scenario: Thu hep bang chu truoc khi doi chieu mat
    Given bai nhac ten 2 giang vien trong danh muc 200 nguoi
    When chon ung vien doi chieu
    Then chi doi chieu dung 2 nguoi do
    And khong do ca danh sach

  Scenario: Mat mo nho nghieng khau trang thi bo qua
    Given mat 30px va mo
    When dinh danh
    Then bo qua doi chieu
    And chi ghi nhan so khuon mat

  Scenario: Ten AI trich phai co doan trich bang chung
    Given cau tra loi muon neu ten "Nguyen Van A"
    When ten do khong xuat hien o caption OCR hay bai viet
    Then khong duoc neu ten
    And tra loi "khong co du lieu"

  Scenario: Ten viet tat hoc ham duoc chuan hoa
    Given caption ghi "PGS.TS. Nguyen Van A"
    When chuan hoa
    Then ra ten chuan trong danh muc kem chuc danh
