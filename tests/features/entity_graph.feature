Feature: Graph thuc the noi chu voi anh/video (D20)
  Bao criteria AC-R13 den AC-R17: ten nguoi / su kien / CLB -> anh, video lien quan.

  Scenario: Hoi anh mot giang vien
    Given danh ba co nguoi "Dang Trong Hop" voi anh trang ho so va anh co doan van sat anh neu ten
    When hoi "cho xem anh thay Dang Trong Hop"
    Then muc media la required
    And anh gan ten bang bang chung manh dung dau
    And khong chen anh chi khop CLIP

  Scenario: Goi bang ten dem va ten
    Given danh ba co dung 1 nguoi ten "Nguyen Thi Cam Ngoan"
    When hoi "anh co Cam Ngoan"
    Then nhan ra nguoi "Nguyen Thi Cam Ngoan"

  Scenario: Chuoi su kien nhieu nam
    Given "IT Festival" co anh cac nam 2016, 2023, 2025
    When hoi "anh bao tuong IT Festival 2023"
    Then anh cua bai nam 2023 dung truoc anh cac nam khac

  Scenario: Ten viet tat cua CLB
    Given CLB "Tin hoc HIT" duoc goi la "CLB HIT" o bai khac
    When hoi "CLB HIT co hoat dong gi"
    Then nhan ra 1 thuc the CLB duy nhat

  Scenario: Canh luon kem bang chung
    Given anh co doan van sat anh "ThS. Nguyen Thi Cam Ngoan trao giai"
    When dung graph cho bai
    Then canh anh -> nguoi luu nguon context, trong so 1.0 va doan trich chua ten

  Scenario: Cau hoi khong nhac thuc the
    Given hoi "hoc phi nganh cong nghe thong tin bao nhieu"
    Then khong co thuc the nao duoc nhan
    And ket qua tim chu giong khi tat graph

  Scenario: Thuc the lien quan cho cau hoi tong hop
    Given nguoi "Dang Trong Hop" duoc neu cung tieu de voi "cuoc thi FPT Hackathon" o 4 bai
    When hoi "thay Dang Trong Hop tham gia nhung su kien nao"
    Then ngu canh LLM co dong graph liet ke "cuoc thi FPT Hackathon (4 bai)" trong nhom su kien

  Scenario: Danh sach sinh vien khong sinh canh
    Given file dinh kem neu 200 ten sinh vien chi trong than bai
    When tinh canh thuc the - thuc the
    Then khong co cap nao giua cac sinh vien do

  Scenario: Hoi anh 2 thuc the cung luc
    Given anh co doan van sat anh neu ca "Pham Van Ha" va "CLB Tin hoc HIT"
    When hoi "anh thay Pham Van Ha tai CLB Tin hoc HIT"
    Then anh gan ca hai thuc the dung dau
