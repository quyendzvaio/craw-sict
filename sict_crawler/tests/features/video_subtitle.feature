Feature: Video keyframe va subtitle-only
  Bao criteria AC-D1 den AC-D5: re hon nhieu so voi soi toan bo video.

  Scenario: Video dai chi giu toi da 20 keyframe
    Given video 10 phut cat theo canh va lay mau thua
    When trich keyframe
    Then so frame khong qua 20
    And moi frame chay qua dung pipeline anh

  Scenario: Co phu de san thi lay lam transcript
    Given video co phu de tieng Viet do nguoi dang tai
    When lay transcript
    Then transcript lay ban nguoi dang
    And do dai khong qua 2000 ky tu

  Scenario: Khong co phu de thi bo qua khong nhan dang giong noi
    Given video khong co bat ky phu de nao
    When lay transcript
    Then transcript rong
    And khong chay nhan dang giong noi

  Scenario: Chi uu tien ban nguoi dang truoc ban tu dong
    Given video co ca ban nguoi dang va ban tu dong
    When lay transcript
    Then dung ban nguoi dang

  Scenario: Video bi chan thi bo qua em khong sap
    Given YouTube doi dang nhap tu IP may chu
    When xu ly video
    Then bo qua video va ghi log
    And trang bai van duoc index bang chu

  Scenario: Nguoi xuat hien 2 giay co the lot keyframe
    Given nguoi chi len hinh 2 giay giua 2 moc lay mau
    When trich keyframe
    Then he thong duoc phep khong thay nguoi do
    And khong cam ket bat duoc moi nguoi
