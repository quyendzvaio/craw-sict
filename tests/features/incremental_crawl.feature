Feature: Crawl tang dan
  Bao criteria AC-E1 den AC-E10: khong bao gio full, kem doi soat nhe.

  Scenario: Bai moi duoc crawl bai cu bo qua
    Given trang chu co lien ket bai moi chua tung thay
    When chay chu ky tang dan
    Then bai moi duoc xu ly
    And bai cu khong tai lai

  Scenario: Bai sua thi xu ly lai bai khong doi thi thoi
    Given bai A doi noi dung va bai B giu nguyen hash
    When kiem tra lai
    Then bai A xu ly lai
    And bai B chi cham thoi gian kiem tra

  Scenario: Bai 404 thi xoa bai loi mang thi giu
    Given bai X tra 404 va bai Y mang rot giua chung
    When kiem tra lai
    Then bai X bi xoa kem vector
    And bai Y duoc giu lai va dem loi

  Scenario: Xoa hang loat bat thuong thi dung lai
    Given 25 bai bi xoa trong 100 bai kiem tra
    When danh gia cau chi
    Then dung chu ky de nguoi kiem tra

  Scenario: Media chi xu ly cai moi
    Given bai sua them 1 anh moi giu 3 anh cu
    When xu ly tang dan
    Then chi 1 anh moi chay OCR va embedding
    And 3 anh cu chi them ban ghi xuat hien

  Scenario: Doi soat dem chi quet ID va hash
    Given luot doi soat hang dem
    When quet
    Then khong tai lai noi dung day du

  Scenario: Doi luot xem hay sidebar khong tinh la sua bai
    Given bai A chi doi so luot xem va khoi tin lien quan
    When kiem tra lai
    Then bai A khong xu ly lai

  Scenario: URL loi tam thoi duoc thu lai khong mat
    Given URL U timeout o lan crawl dau
    When chay cac chu ky tiep theo
    Then U duoc thu lai voi thoi gian cho tang dan
    And sau 5 lan van loi thi U vao danh sach chet

  Scenario: Bai khong doi thi gian chu ky kiem tra
    Given bai B khong doi qua 3 lan kiem tra
    When tinh lich kiem tra tiep
    Then chu ky cua B gap doi moi lan va khong qua 30 ngay

  Scenario: OCR rong khong bi chay lai mai
    Given frame video co phu de nhung OCR khong ra chu
    When chay backlog OCR 3 chu ky lien tiep
    Then frame chi duoc OCR 1 lan
    And khong sinh them vector caption trung

  Scenario: Loi mot trang khong lam hong cac trang sau
    Given trang thu 5 trong chu ky gap loi ghi du lieu
    When chu ky tiep tuc
    Then cac trang tu thu 6 van duoc luu binh thuong
