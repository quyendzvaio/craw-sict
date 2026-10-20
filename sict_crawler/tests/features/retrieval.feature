Feature: Retrieval ket hop chu anh metadata
  Bao criteria AC-R1 den AC-R7. Router media o media_router.feature, loc metadata o metadata_filter.feature.

  Scenario: Hoi su kien ra ca chu va anh co bang chung
    Given hoi "le khai giang 2024"
    When retrieval chay
    Then co it nhat 1 doan van co URL va doan trich
    And co it nhat 1 media co URL va caption

  Scenario: Hoi ten nguoi ra anh cua nguoi do
    Given caption anh da nhuong chung khong gian chu
    When hoi "thay Nguyen Van A"
    Then ket qua media chua anh caption nhac ten do

  Scenario: Mot video chi tinh 1 diem
    Given video co 20 frame deu khop
    When xep hang
    Then video do chi giu 1 vi tri trong top

  Scenario: Ton trong top_k va khong bia ket qua
    Given hoi tu khoa khong ton tai "xyzabc123"
    When retrieval chay voi top_k 5
    Then tra khong qua 5 ket qua
    And tap rong thi tra rong khong bia

  Scenario: Cau hoi viet tat duoc mo rong truoc khi tim
    Given hoi "TS A khoa CNTT"
    When viet lai cau hoi
    Then cau mo rong chua ten day du co dau

  Scenario: Ket qua trung duoc gom lai
    Given cung anh xuat hien o 2 bai deu khop
    When hop nhat
    Then chi hien 1 dong media kem ca 2 nguon

  Scenario: Tu khoa khong dau khop bai co dau
    Given bai co cum "tuyển sinh"
    When hoi "tuyen sinh"
    Then bai do nam trong ket qua

  Scenario: Hoi tieng Viet ra anh qua nhanh chu sang anh
    Given anh hoi cho khong co caption chu
    When hoi "anh hoi cho trien lam"
    Then anh do nam trong ket qua media
